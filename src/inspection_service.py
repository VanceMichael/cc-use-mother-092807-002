"""口岸食品准入查验与退运销毁闭环服务。

从境外生产企业和产品资格开始，保存舱单、箱货、批次、温控记录、
审单规则、查验任务、样品链、检测结论、处置决定和企业整改：

- 风险规则换版只影响之后形成的决定，已放行、退运或销毁的事实不可覆盖；
- 同一集装箱可拆成多个货批，整票扣留与局部放行按货量互斥；
- 抽样拆分与复检保持货量与样品去向一致；
- 同一渠道回执重送只返回原结果，编号相同但内容不同则暂停相关批次；
- 企业只能查看自己的材料，取样人不能独自确认最终合格，
  跨部门协查按履职范围开放；
- 服务恢复后继续超期样品、待处置货物和整改复核任务；
- 任一放行或销毁决定可还原当时的资格、规则、检测和批准依据。

时间由调用方传入，状态可整体快照并恢复，不依赖外部系统。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

DOMAIN = "border-food-inspection"


# ---------------------------------------------------------------------------
# 参与方与错误
# ---------------------------------------------------------------------------


class Role(Enum):
    """参与方角色。"""

    CUSTOMS = "海关食品监管人员"
    ENTERPRISE = "企业"
    LAB = "实验室人员"
    EXECUTOR = "退运与销毁执行单位"
    COLLABORATOR = "跨部门协查人员"


@dataclass(frozen=True)
class Principal:
    """一次操作的调用方身份；duties 为跨部门协查的履职范围。"""

    actor_id: str
    role: Role
    org_id: str
    duties: tuple[str, ...] = ()


class DomainError(Exception):
    """领域操作被拒绝的基类。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class QualificationError(DomainError):
    """境外企业或产品资格不满足准入要求。"""


class ConflictError(DomainError):
    """与既有事实冲突，例如扣留与放行互斥、数量超界。"""


class ReceiptConflictError(ConflictError):
    """渠道回执编号相同但内容不同。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class PermissionDeniedError(PermissionError):
    """越权访问或越权操作。"""


# ---------------------------------------------------------------------------
# 资格、规则与单证
# ---------------------------------------------------------------------------


class RegistrationStatus(Enum):
    ACTIVE = "有效"
    SUSPENDED = "暂停"
    CANCELLED = "注销"


@dataclass(frozen=True)
class RegistrationVersion:
    """境外生产企业注册的一个版本，历史版本永不覆盖。"""

    registration_id: str
    version: int
    enterprise_id: str
    enterprise_name: str
    country: str
    categories: tuple[str, ...]
    status: RegistrationStatus
    valid_until: date
    changed_at: datetime
    reason: str


@dataclass(frozen=True)
class QualificationVersion:
    """产品资格的一个版本。"""

    qualification_id: str
    version: int
    registration_id: str
    category: str
    hs_code: str
    specs: tuple[str, ...]
    status: RegistrationStatus
    changed_at: datetime
    reason: str


@dataclass(frozen=True)
class RiskRuleSet:
    """审单与风险规则的一个版本，换版只影响之后形成的决定。"""

    version: int
    effective_from: datetime
    required_docs: tuple[str, ...]
    high_risk_categories: tuple[str, ...]
    sample_sla_days: int
    rectification_deadline_days: int
    cold_chain_categories: tuple[str, ...]
    temperature_min: Decimal
    temperature_max: Decimal


@dataclass(frozen=True)
class Manifest:
    manifest_id: str
    voyage: str
    importer_id: str
    declared_at: datetime


@dataclass(frozen=True)
class Container:
    container_no: str
    manifest_id: str
    declared_quantity: Decimal


@dataclass(frozen=True)
class TemperatureRecord:
    record_id: str
    container_no: str
    recorded_at: datetime
    celsius: Decimal


# ---------------------------------------------------------------------------
# 货批与货量台账
# ---------------------------------------------------------------------------


@dataclass
class LineState:
    """一个规格的货量台账：在控=总量-已放行-已扣留-已退运-已销毁。"""

    spec: str
    quantity: Decimal
    released: Decimal = Decimal("0")
    detained: Decimal = Decimal("0")
    returned: Decimal = Decimal("0")
    destroyed: Decimal = Decimal("0")

    @property
    def pending(self) -> Decimal:
        return self.quantity - self.released - self.detained - self.returned - self.destroyed


@dataclass
class GoodsBatch:
    batch_id: str
    container_no: str
    manifest_id: str
    importer_id: str
    registration_id: str
    qualification_id: str
    category: str
    hs_code: str
    lines: dict[str, LineState]
    declared_at: datetime
    review_passed: bool = False
    suspended: bool = False
    suspend_reason: str = ""


class TaskKind(Enum):
    DOCUMENT_REVIEW = "审单"
    RECTIFICATION_REVIEW = "整改复核"


class TaskStatus(Enum):
    PENDING = "待办"
    DONE = "办结"


@dataclass
class InspectionTask:
    task_id: str
    kind: TaskKind
    ref_id: str
    assignee: str
    created_at: datetime
    due_at: datetime | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# 样品链与检测结论
# ---------------------------------------------------------------------------


class PortionKind(Enum):
    LAB = "检样"
    RETENTION = "留样"
    RETEST = "复检样"


@dataclass
class SamplePortion:
    """样品的一份去向，数量守恒：各份之和始终等于取样量。"""

    portion_id: str
    kind: PortionKind
    quantity: Decimal
    location: str


class SampleStatus(Enum):
    IN_LAB = "在检"
    CONCLUDED = "已出结论"


@dataclass
class Sample:
    sample_id: str
    batch_id: str
    spec: str
    drawn_quantity: Decimal
    sampler_id: str
    lab_id: str
    active_lab_id: str
    drawn_at: datetime
    lab_deadline: datetime
    rule_version: int
    portions: dict[str, SamplePortion]
    status: SampleStatus = SampleStatus.IN_LAB


class ItemResult(Enum):
    PASS = "合格"
    FAIL = "不合格"


@dataclass(frozen=True)
class TestItem:
    """一个检测项目；不合格时必须标明受影响规格。"""

    name: str
    result: ItemResult
    affected_specs: tuple[str, ...] = ()


@dataclass
class TestConclusion:
    conclusion_id: str
    sample_id: str
    lab_id: str
    items: tuple[TestItem, ...]
    concluded_at: datetime
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @property
    def passed(self) -> bool:
        return all(item.result is ItemResult.PASS for item in self.items)

    @property
    def failed_specs(self) -> frozenset[str]:
        return frozenset(
            spec
            for item in self.items
            if item.result is ItemResult.FAIL
            for spec in item.affected_specs
        )


# ---------------------------------------------------------------------------
# 处置决定、批准与证明
# ---------------------------------------------------------------------------


class DecisionType(Enum):
    RELEASE = "放行"
    DETAIN = "扣留"
    LIFT_DETENTION = "解除扣留"
    RETURN = "退运"
    DESTROY = "销毁"


@dataclass(frozen=True)
class DecisionLine:
    spec: str
    quantity: Decimal


@dataclass(frozen=True)
class Approval:
    approver_id: str
    approved_at: datetime


@dataclass(frozen=True)
class DecisionBasis:
    """决定形成时采用的资格、规则与检测依据快照。"""

    registration_version: int
    qualification_version: int
    rule_version: int
    conclusion_ids: tuple[str, ...]
    temperature_record_ids: tuple[str, ...]


@dataclass
class DisposalDecision:
    """处置决定一旦形成不可覆盖；退运销毁须他人批准并以证明闭环。"""

    decision_id: str
    batch_id: str
    decision_type: DecisionType
    lines: tuple[DecisionLine, ...]
    basis: DecisionBasis
    decided_by: str
    decided_at: datetime
    approvals: tuple[Approval, ...] = ()
    approved: bool = False
    executed: bool = False


@dataclass(frozen=True)
class DisposalCertificate:
    certificate_id: str
    decision_id: str
    executor_id: str
    lines: tuple[DecisionLine, ...]
    proof: str
    issued_at: datetime


@dataclass(frozen=True)
class ChannelReceipt:
    channel: str
    receipt_no: str
    fingerprint: str
    batch_ids: tuple[str, ...]
    received_at: datetime
    result: str


class RectificationStatus(Enum):
    OPEN = "待整改"
    SUBMITTED = "待复核"
    PASSED = "复核通过"
    FAILED = "复核未通过"


@dataclass
class Rectification:
    rectification_id: str
    registration_id: str
    issue: str
    opened_at: datetime
    deadline: datetime
    status: RectificationStatus = RectificationStatus.OPEN
    evidence: str = ""
    reviewed_by: str | None = None


@dataclass(frozen=True)
class RecoveryPlan:
    """服务恢复后继续办理的工作。"""

    overdue_samples: tuple[str, ...]
    pending_disposal_batches: tuple[str, ...]
    pending_rectification_reviews: tuple[str, ...]


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class InspectionService:
    """口岸食品准入查验与退运销毁闭环服务。"""

    def __init__(self) -> None:
        self._registrations: dict[str, list[RegistrationVersion]] = {}
        self._qualifications: dict[str, list[QualificationVersion]] = {}
        self._manifests: dict[str, Manifest] = {}
        self._containers: dict[str, Container] = {}
        self._temperature: dict[str, list[TemperatureRecord]] = {}
        self._batches: dict[str, GoodsBatch] = {}
        self._rules: dict[int, RiskRuleSet] = {}
        self._tasks: dict[str, InspectionTask] = {}
        self._samples: dict[str, Sample] = {}
        self._conclusions: dict[str, TestConclusion] = {}
        self._decisions: dict[str, DisposalDecision] = {}
        self._certificates: dict[str, DisposalCertificate] = {}
        self._receipts: dict[tuple[str, str], ChannelReceipt] = {}
        self._rectifications: dict[str, Rectification] = {}

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _qty(value: Any) -> Decimal:
        return value if isinstance(value, Decimal) else Decimal(str(value))

    @staticmethod
    def _require_role(principal: Principal, *roles: Role) -> None:
        if principal.role not in roles:
            allowed = "、".join(role.value for role in roles)
            raise PermissionDeniedError(f"{principal.role.value}无权执行该操作，需：{allowed}")

    def _registration(self, registration_id: str) -> RegistrationVersion:
        history = self._registrations.get(registration_id)
        if not history:
            raise NotFoundError(f"境外生产企业注册不存在：{registration_id}")
        return history[-1]

    def _qualification(self, qualification_id: str) -> QualificationVersion:
        history = self._qualifications.get(qualification_id)
        if not history:
            raise NotFoundError(f"产品资格不存在：{qualification_id}")
        return history[-1]

    def _batch(self, batch_id: str) -> GoodsBatch:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"货批不存在：{batch_id}")
        return batch

    def _sample(self, sample_id: str) -> Sample:
        sample = self._samples.get(sample_id)
        if sample is None:
            raise NotFoundError(f"样品不存在：{sample_id}")
        return sample

    def _rules_now(self, now: datetime) -> RiskRuleSet:
        effective = [rules for rules in self._rules.values() if rules.effective_from <= now]
        if not effective:
            raise StateError("无生效风险规则")
        return max(effective, key=lambda rules: rules.version)

    @staticmethod
    def _ensure_not_suspended(batch: GoodsBatch) -> None:
        if batch.suspended:
            raise StateError(f"批次{batch.batch_id}已暂停：{batch.suspend_reason}")

    def _owns_batch(self, principal: Principal, batch: GoodsBatch) -> bool:
        if batch.importer_id == principal.org_id:
            return True
        return self._registration(batch.registration_id).enterprise_id == principal.org_id

    def _status_of(self, batch: GoodsBatch) -> str:
        if batch.suspended:
            return "暂停"
        lines = list(batch.lines.values())
        if all(line.released == line.quantity for line in lines):
            return "已放行"
        if all(line.pending == 0 and line.detained == 0 for line in lines):
            return "已处置"
        if any(line.detained > 0 for line in lines):
            return "扣留中"
        if any(line.released > 0 for line in lines):
            return "部分放行"
        return "在检" if batch.review_passed else "待审单"

    # -- 境外企业和产品资格 -------------------------------------------------

    def register_manufacturer(
        self,
        principal: Principal,
        registration_id: str,
        *,
        enterprise_id: str,
        enterprise_name: str,
        country: str,
        categories: tuple[str, ...],
        valid_until: date,
        now: datetime,
    ) -> RegistrationVersion:
        """海关完成境外生产企业注册审核后登记，初始版本为1。"""
        self._require_role(principal, Role.CUSTOMS)
        if registration_id in self._registrations:
            raise ConflictError(f"注册编号已存在：{registration_id}")
        version = RegistrationVersion(
            registration_id=registration_id,
            version=1,
            enterprise_id=enterprise_id,
            enterprise_name=enterprise_name,
            country=country,
            categories=tuple(categories),
            status=RegistrationStatus.ACTIVE,
            valid_until=valid_until,
            changed_at=now,
            reason="初始注册",
        )
        self._registrations[registration_id] = [version]
        return version

    def change_registration(
        self,
        principal: Principal,
        registration_id: str,
        now: datetime,
        *,
        status: RegistrationStatus | None = None,
        categories: tuple[str, ...] | None = None,
        valid_until: date | None = None,
        reason: str = "",
    ) -> RegistrationVersion:
        """变更注册状态或范围，追加新版本而不覆盖历史。"""
        self._require_role(principal, Role.CUSTOMS)
        history = self._registrations.get(registration_id)
        if not history:
            raise NotFoundError(f"境外生产企业注册不存在：{registration_id}")
        current = history[-1]
        version = RegistrationVersion(
            registration_id=registration_id,
            version=current.version + 1,
            enterprise_id=current.enterprise_id,
            enterprise_name=current.enterprise_name,
            country=current.country,
            categories=tuple(categories) if categories is not None else current.categories,
            status=status if status is not None else current.status,
            valid_until=valid_until if valid_until is not None else current.valid_until,
            changed_at=now,
            reason=reason,
        )
        history.append(version)
        return version

    def registration_at(self, registration_id: str, version: int | None = None) -> RegistrationVersion:
        """读取注册的最新或指定历史版本。"""
        history = self._registrations.get(registration_id)
        if not history:
            raise NotFoundError(f"境外生产企业注册不存在：{registration_id}")
        if version is None:
            return history[-1]
        for entry in history:
            if entry.version == version:
                return entry
        raise NotFoundError(f"注册版本不存在：{registration_id}@{version}")

    def qualify_product(
        self,
        principal: Principal,
        qualification_id: str,
        registration_id: str,
        *,
        category: str,
        hs_code: str,
        specs: tuple[str, ...],
        now: datetime,
    ) -> QualificationVersion:
        """核准产品资格，须落在企业注册范围内。"""
        self._require_role(principal, Role.CUSTOMS)
        if qualification_id in self._qualifications:
            raise ConflictError(f"产品资格编号已存在：{qualification_id}")
        registration = self._registration(registration_id)
        if registration.status is not RegistrationStatus.ACTIVE:
            raise QualificationError("境外生产企业注册无效，不能核准产品资格")
        if category not in registration.categories:
            raise QualificationError("产品类别未在注册范围内")
        version = QualificationVersion(
            qualification_id=qualification_id,
            version=1,
            registration_id=registration_id,
            category=category,
            hs_code=hs_code,
            specs=tuple(specs),
            status=RegistrationStatus.ACTIVE,
            changed_at=now,
            reason="初始核准",
        )
        self._qualifications[qualification_id] = [version]
        return version

    def change_qualification(
        self,
        principal: Principal,
        qualification_id: str,
        now: datetime,
        *,
        status: RegistrationStatus | None = None,
        specs: tuple[str, ...] | None = None,
        reason: str = "",
    ) -> QualificationVersion:
        """变更产品资格，追加新版本而不覆盖历史。"""
        self._require_role(principal, Role.CUSTOMS)
        history = self._qualifications.get(qualification_id)
        if not history:
            raise NotFoundError(f"产品资格不存在：{qualification_id}")
        current = history[-1]
        version = QualificationVersion(
            qualification_id=qualification_id,
            version=current.version + 1,
            registration_id=current.registration_id,
            category=current.category,
            hs_code=current.hs_code,
            specs=tuple(specs) if specs is not None else current.specs,
            status=status if status is not None else current.status,
            changed_at=now,
            reason=reason,
        )
        history.append(version)
        return version

    def qualification_at(self, qualification_id: str, version: int | None = None) -> QualificationVersion:
        """读取产品资格的最新或指定历史版本。"""
        history = self._qualifications.get(qualification_id)
        if not history:
            raise NotFoundError(f"产品资格不存在：{qualification_id}")
        if version is None:
            return history[-1]
        for entry in history:
            if entry.version == version:
                return entry
        raise NotFoundError(f"产品资格版本不存在：{qualification_id}@{version}")

    # -- 风险规则版本 --------------------------------------------------------

    def publish_rules(self, principal: Principal, rules: RiskRuleSet) -> RiskRuleSet:
        """发布新版风险规则，版本只能递增，旧版本永不覆盖。"""
        self._require_role(principal, Role.CUSTOMS)
        if rules.version in self._rules or any(rules.version < v for v in self._rules):
            raise ConflictError("规则版本必须递增")
        self._rules[rules.version] = rules
        return rules

    def rules_version(self, version: int) -> RiskRuleSet:
        rules = self._rules.get(version)
        if rules is None:
            raise NotFoundError(f"规则版本不存在：{version}")
        return rules

    # -- 舱单、箱货与批次 ----------------------------------------------------

    def declare_manifest(
        self,
        principal: Principal,
        manifest_id: str,
        *,
        voyage: str,
        importer_id: str,
        now: datetime,
    ) -> Manifest:
        self._require_role(principal, Role.CUSTOMS, Role.ENTERPRISE)
        if principal.role is Role.ENTERPRISE and principal.org_id != importer_id:
            raise PermissionDeniedError("企业只能申报自己的舱单")
        if manifest_id in self._manifests:
            raise ConflictError(f"舱单编号已存在：{manifest_id}")
        manifest = Manifest(manifest_id=manifest_id, voyage=voyage, importer_id=importer_id, declared_at=now)
        self._manifests[manifest_id] = manifest
        return manifest

    def register_container(
        self,
        principal: Principal,
        container_no: str,
        manifest_id: str,
        declared_quantity: Any,
    ) -> Container:
        manifest = self._manifests.get(manifest_id)
        if manifest is None:
            raise NotFoundError(f"舱单不存在：{manifest_id}")
        self._require_role(principal, Role.CUSTOMS, Role.ENTERPRISE)
        if principal.role is Role.ENTERPRISE and principal.org_id != manifest.importer_id:
            raise PermissionDeniedError("企业只能登记自己舱单下的集装箱")
        if container_no in self._containers:
            raise ConflictError(f"集装箱已登记：{container_no}")
        quantity = self._qty(declared_quantity)
        if quantity <= 0:
            raise StateError("集装箱申报货量必须为正")
        container = Container(container_no=container_no, manifest_id=manifest_id, declared_quantity=quantity)
        self._containers[container_no] = container
        return container

    def record_temperature(
        self,
        principal: Principal,
        record_id: str,
        container_no: str,
        *,
        celsius: Any,
        recorded_at: datetime,
    ) -> TemperatureRecord:
        container = self._containers.get(container_no)
        if container is None:
            raise NotFoundError(f"集装箱未登记：{container_no}")
        importer_id = self._manifests[container.manifest_id].importer_id
        self._require_role(principal, Role.CUSTOMS, Role.ENTERPRISE)
        if principal.role is Role.ENTERPRISE and principal.org_id != importer_id:
            raise PermissionDeniedError("企业只能记录自己货物的温控")
        if any(
            record.record_id == record_id
            for records in self._temperature.values()
            for record in records
        ):
            raise ConflictError(f"温控记录编号已存在：{record_id}")
        record = TemperatureRecord(
            record_id=record_id,
            container_no=container_no,
            recorded_at=recorded_at,
            celsius=self._qty(celsius),
        )
        self._temperature.setdefault(container_no, []).append(record)
        return record

    def declare_batch(
        self,
        principal: Principal,
        batch_id: str,
        container_no: str,
        registration_id: str,
        qualification_id: str,
        *,
        category: str,
        hs_code: str,
        lines: dict[str, Any],
        now: datetime,
    ) -> GoodsBatch:
        """申报货批：校验资格、规格与箱货量，并按现行规则生成审单任务。"""
        if batch_id in self._batches:
            raise ConflictError(f"批次编号已存在：{batch_id}")
        container = self._containers.get(container_no)
        if container is None:
            raise NotFoundError(f"集装箱未登记：{container_no}")
        manifest = self._manifests[container.manifest_id]
        self._require_role(principal, Role.CUSTOMS, Role.ENTERPRISE)
        if principal.role is Role.ENTERPRISE and principal.org_id != manifest.importer_id:
            raise PermissionDeniedError("企业只能申报自己的货批")
        registration = self._registration(registration_id)
        if registration.status is not RegistrationStatus.ACTIVE or registration.valid_until < now.date():
            raise QualificationError("境外生产企业注册无效")
        if category not in registration.categories:
            raise QualificationError("产品类别未在注册范围内")
        qualification = self._qualification(qualification_id)
        if qualification.registration_id != registration_id:
            raise QualificationError("产品资格与生产企业不一致")
        if qualification.status is not RegistrationStatus.ACTIVE:
            raise QualificationError("产品资格无效")
        if category != qualification.category or hs_code != qualification.hs_code:
            raise QualificationError("产品资格与申报不一致")
        parsed = {spec: self._qty(qty) for spec, qty in lines.items()}
        if not parsed or any(qty <= 0 for qty in parsed.values()):
            raise StateError("批次货量必须为正")
        unknown = sorted(set(parsed) - set(qualification.specs))
        if unknown:
            raise QualificationError(f"规格未获产品资格：{unknown}")
        committed = sum(
            line.quantity
            for other in self._batches.values()
            if other.container_no == container_no
            for line in other.lines.values()
        )
        if committed + sum(parsed.values(), Decimal("0")) > container.declared_quantity:
            raise ConflictError("超出集装箱申报货量")
        rules = self._rules_now(now)
        batch = GoodsBatch(
            batch_id=batch_id,
            container_no=container_no,
            manifest_id=container.manifest_id,
            importer_id=manifest.importer_id,
            registration_id=registration_id,
            qualification_id=qualification_id,
            category=category,
            hs_code=hs_code,
            lines={spec: LineState(spec=spec, quantity=qty) for spec, qty in parsed.items()},
            declared_at=now,
        )
        self._batches[batch_id] = batch
        task = InspectionTask(
            task_id=f"REV-{batch_id}",
            kind=TaskKind.DOCUMENT_REVIEW,
            ref_id=batch_id,
            assignee=Role.CUSTOMS.value,
            created_at=now,
            detail="所需单证：" + "、".join(rules.required_docs),
        )
        self._tasks[task.task_id] = task
        return batch

    def complete_document_review(
        self,
        principal: Principal,
        task_id: str,
        passed: bool,
        now: datetime,
    ) -> InspectionTask:
        """办结审单任务，审单通过是抽样与放行的前提。"""
        self._require_role(principal, Role.CUSTOMS)
        task = self._tasks.get(task_id)
        if task is None or task.kind is not TaskKind.DOCUMENT_REVIEW:
            raise NotFoundError(f"审单任务不存在：{task_id}")
        if task.status is TaskStatus.DONE:
            raise StateError("任务已办结")
        task.status = TaskStatus.DONE
        task.result = "通过" if passed else "不通过"
        self._batch(task.ref_id).review_passed = passed
        return task

    # -- 抽样与样品链 --------------------------------------------------------

    def _drawn_quantity(self, batch_id: str, spec: str) -> Decimal:
        return sum(
            (s.drawn_quantity for s in self._samples.values() if s.batch_id == batch_id and s.spec == spec),
            Decimal("0"),
        )

    def draw_sample(
        self,
        principal: Principal,
        sample_id: str,
        batch_id: str,
        spec: str,
        quantity: Any,
        lab_id: str,
        now: datetime,
    ) -> Sample:
        """从货批取样送检，取样量不得超过尚未放行或处置的货量。"""
        self._require_role(principal, Role.CUSTOMS)
        if sample_id in self._samples:
            raise ConflictError(f"样品编号已存在：{sample_id}")
        batch = self._batch(batch_id)
        self._ensure_not_suspended(batch)
        if not batch.review_passed:
            raise StateError("审单未通过，不得抽样")
        line = batch.lines.get(spec)
        if line is None:
            raise NotFoundError(f"批次无该规格：{spec}")
        qty = self._qty(quantity)
        available = line.quantity - line.released - line.returned - line.destroyed - self._drawn_quantity(batch_id, spec)
        if qty <= 0 or qty > available:
            raise StateError("可抽样货量不足")
        rules = self._rules_now(now)
        portion = SamplePortion(
            portion_id=f"{sample_id}-P1",
            kind=PortionKind.LAB,
            quantity=qty,
            location=f"实验室:{lab_id}",
        )
        sample = Sample(
            sample_id=sample_id,
            batch_id=batch_id,
            spec=spec,
            drawn_quantity=qty,
            sampler_id=principal.actor_id,
            lab_id=lab_id,
            active_lab_id=lab_id,
            drawn_at=now,
            lab_deadline=now + timedelta(days=rules.sample_sla_days),
            rule_version=rules.version,
            portions={portion.portion_id: portion},
        )
        self._samples[sample_id] = sample
        return sample

    @staticmethod
    def _require_sample_handler(principal: Principal, sample: Sample) -> None:
        if principal.role is Role.CUSTOMS:
            return
        if principal.role is Role.LAB and principal.org_id == sample.active_lab_id:
            return
        raise PermissionDeniedError("只有海关或承检实验室可以操作样品")

    @staticmethod
    def _assert_sample_integrity(sample: Sample) -> None:
        total = sum((p.quantity for p in sample.portions.values()), Decimal("0"))
        if total != sample.drawn_quantity:
            raise StateError(f"样品{sample.sample_id}数量不守恒")

    def split_sample(
        self,
        principal: Principal,
        sample_id: str,
        new_portion_id: str,
        from_portion_id: str,
        kind: PortionKind,
        quantity: Any,
        location: str,
    ) -> SamplePortion:
        """拆分样品（如分留样），数量在份间转移，总量保持守恒。"""
        sample = self._sample(sample_id)
        self._require_sample_handler(principal, sample)
        source = sample.portions.get(from_portion_id)
        if source is None:
            raise NotFoundError(f"样品份不存在：{from_portion_id}")
        if new_portion_id in sample.portions:
            raise ConflictError(f"样品份编号已存在：{new_portion_id}")
        qty = self._qty(quantity)
        if qty <= 0 or qty > source.quantity:
            raise StateError("拆分数量超出样品余量")
        source.quantity -= qty
        portion = SamplePortion(portion_id=new_portion_id, kind=kind, quantity=qty, location=location)
        sample.portions[new_portion_id] = portion
        self._assert_sample_integrity(sample)
        return portion

    def request_retest(
        self,
        principal: Principal,
        sample_id: str,
        new_portion_id: str,
        quantity: Any,
        lab_id: str,
        now: datetime,
    ) -> SamplePortion:
        """发起复检：从留样中分出复检样送检，样品回到在检状态。"""
        self._require_role(principal, Role.CUSTOMS)
        sample = self._sample(sample_id)
        if new_portion_id in sample.portions:
            raise ConflictError(f"样品份编号已存在：{new_portion_id}")
        qty = self._qty(quantity)
        retention = next(
            (p for p in sample.portions.values() if p.kind is PortionKind.RETENTION and p.quantity >= qty),
            None,
        )
        if qty <= 0 or retention is None:
            raise StateError("留样不足，无法复检")
        retention.quantity -= qty
        portion = SamplePortion(
            portion_id=new_portion_id,
            kind=PortionKind.RETEST,
            quantity=qty,
            location=f"实验室:{lab_id}",
        )
        sample.portions[new_portion_id] = portion
        sample.active_lab_id = lab_id
        sample.status = SampleStatus.IN_LAB
        sample.lab_deadline = now + timedelta(days=self._rules_now(now).sample_sla_days)
        self._assert_sample_integrity(sample)
        return portion

    def dispose_portion(
        self,
        principal: Principal,
        sample_id: str,
        portion_id: str,
        location: str,
    ) -> SamplePortion:
        """登记样品份最终去向（留存、销毁、退还等），数量不变。"""
        sample = self._sample(sample_id)
        self._require_sample_handler(principal, sample)
        portion = sample.portions.get(portion_id)
        if portion is None:
            raise NotFoundError(f"样品份不存在：{portion_id}")
        if not location.strip():
            raise StateError("样品去向不能为空")
        portion.location = location
        return portion

    def sample_integrity(self, sample_id: str) -> bool:
        """校验样品各份数量之和等于取样量。"""
        sample = self._sample(sample_id)
        total = sum((p.quantity for p in sample.portions.values()), Decimal("0"))
        return total == sample.drawn_quantity

    # -- 检测结论与确认 ------------------------------------------------------

    def submit_conclusion(
        self,
        principal: Principal,
        conclusion_id: str,
        sample_id: str,
        items: tuple[TestItem, ...],
        now: datetime,
    ) -> TestConclusion:
        """承检实验室提交检测结论，不合格项目必须标明受影响规格。"""
        sample = self._sample(sample_id)
        if principal.role is not Role.LAB or principal.org_id != sample.active_lab_id:
            raise PermissionDeniedError("只能由承检实验室提交结论")
        if conclusion_id in self._conclusions:
            raise ConflictError(f"结论编号已存在：{conclusion_id}")
        if sample.status is not SampleStatus.IN_LAB:
            raise StateError("样品不在检测状态")
        if not items:
            raise StateError("检测项目不能为空")
        batch = self._batch(sample.batch_id)
        for item in items:
            if item.result is ItemResult.FAIL:
                if not item.affected_specs:
                    raise StateError("不合格项目必须标明受影响规格")
                unknown = sorted(set(item.affected_specs) - set(batch.lines))
                if unknown:
                    raise StateError(f"受影响规格不在批次内：{unknown}")
        conclusion = TestConclusion(
            conclusion_id=conclusion_id,
            sample_id=sample_id,
            lab_id=principal.org_id,
            items=tuple(items),
            concluded_at=now,
        )
        self._conclusions[conclusion_id] = conclusion
        sample.status = SampleStatus.CONCLUDED
        return conclusion

    def confirm_conclusion(
        self,
        principal: Principal,
        conclusion_id: str,
        now: datetime,
    ) -> TestConclusion:
        """海关确认检测结论；取样人不能独自确认最终合格。"""
        self._require_role(principal, Role.CUSTOMS)
        conclusion = self._conclusions.get(conclusion_id)
        if conclusion is None:
            raise NotFoundError(f"检测结论不存在：{conclusion_id}")
        sample = self._sample(conclusion.sample_id)
        if principal.actor_id == sample.sampler_id:
            raise PermissionDeniedError("取样人不能独自确认最终合格")
        if conclusion.confirmed_by is not None:
            raise StateError("结论已确认")
        conclusion.confirmed_by = principal.actor_id
        conclusion.confirmed_at = now
        return conclusion

    def _latest_confirmed(self, batch_id: str) -> list[TestConclusion]:
        """每个样品最新一份已确认结论；复检结论覆盖初检。"""
        by_sample: dict[str, TestConclusion] = {}
        for conclusion in self._conclusions.values():
            if conclusion.confirmed_by is None:
                continue
            sample = self._samples[conclusion.sample_id]
            if sample.batch_id != batch_id:
                continue
            current = by_sample.get(conclusion.sample_id)
            if current is None or (conclusion.confirmed_at or conclusion.concluded_at) > (
                current.confirmed_at or current.concluded_at
            ):
                by_sample[conclusion.sample_id] = conclusion
        return list(by_sample.values())

    # -- 处置决定 ------------------------------------------------------------

    def make_decision(
        self,
        principal: Principal,
        decision_id: str,
        batch_id: str,
        decision_type: DecisionType,
        lines: tuple[tuple[str, Any], ...],
        now: datetime,
    ) -> DisposalDecision:
        """形成放行、扣留、解除扣留、退运或销毁决定。

        决定按货量互斥：已放行、退运或销毁的数量只增不减；
        同一编号重送相同内容返回原决定，内容不同则拒绝。
        """
        self._require_role(principal, Role.CUSTOMS)
        batch = self._batch(batch_id)
        parsed = tuple(
            sorted(
                (DecisionLine(spec=spec, quantity=self._qty(qty)) for spec, qty in lines),
                key=lambda line: line.spec,
            )
        )
        existing = self._decisions.get(decision_id)
        if existing is not None:
            if (
                existing.batch_id == batch_id
                and existing.decision_type is decision_type
                and existing.lines == parsed
                and existing.decided_by == principal.actor_id
            ):
                return existing
            raise ConflictError("决定编号相同但内容不同")
        self._ensure_not_suspended(batch)
        if not parsed:
            raise StateError("决定必须指定规格与数量")
        if len({line.spec for line in parsed}) != len(parsed):
            raise StateError("决定中规格重复")
        for decision_line in parsed:
            if decision_line.spec not in batch.lines:
                raise NotFoundError(f"批次无该规格：{decision_line.spec}")
            if decision_line.quantity <= 0:
                raise StateError("决定数量必须为正")
        rules = self._rules_now(now)
        if decision_type is DecisionType.RELEASE:
            self._apply_release(batch, parsed, rules, now)
        elif decision_type is DecisionType.DETAIN:
            for decision_line in parsed:
                if decision_line.quantity > batch.lines[decision_line.spec].pending:
                    raise ConflictError("扣留数量超出在控货量")
            for decision_line in parsed:
                batch.lines[decision_line.spec].detained += decision_line.quantity
        elif decision_type is DecisionType.LIFT_DETENTION:
            for decision_line in parsed:
                if decision_line.quantity > batch.lines[decision_line.spec].detained:
                    raise ConflictError("解除扣留数量超出已扣留货量")
            for decision_line in parsed:
                batch.lines[decision_line.spec].detained -= decision_line.quantity
        elif decision_type in (DecisionType.RETURN, DecisionType.DESTROY):
            for decision_line in parsed:
                if decision_line.quantity > batch.lines[decision_line.spec].detained:
                    raise ConflictError("处置数量超出已扣留货量")
            for decision_line in parsed:
                line = batch.lines[decision_line.spec]
                line.detained -= decision_line.quantity
                if decision_type is DecisionType.RETURN:
                    line.returned += decision_line.quantity
                else:
                    line.destroyed += decision_line.quantity
        else:
            raise StateError(f"未知的决定类型：{decision_type}")
        basis = DecisionBasis(
            registration_version=self._registration(batch.registration_id).version,
            qualification_version=self._qualification(batch.qualification_id).version,
            rule_version=rules.version,
            conclusion_ids=tuple(c.conclusion_id for c in self._latest_confirmed(batch.batch_id)),
            temperature_record_ids=(
                tuple(record.record_id for record in self._temperature.get(batch.container_no, ()))
                if decision_type is DecisionType.RELEASE and batch.category in rules.cold_chain_categories
                else ()
            ),
        )
        decision = DisposalDecision(
            decision_id=decision_id,
            batch_id=batch_id,
            decision_type=decision_type,
            lines=parsed,
            basis=basis,
            decided_by=principal.actor_id,
            decided_at=now,
            executed=decision_type in (DecisionType.RELEASE, DecisionType.DETAIN, DecisionType.LIFT_DETENTION),
        )
        self._decisions[decision_id] = decision
        return decision

    def _apply_release(
        self,
        batch: GoodsBatch,
        parsed: tuple[DecisionLine, ...],
        rules: RiskRuleSet,
        now: datetime,
    ) -> None:
        if not batch.review_passed:
            raise StateError("审单未通过，不得放行")
        registration = self._registration(batch.registration_id)
        if registration.status is not RegistrationStatus.ACTIVE or registration.valid_until < now.date():
            raise QualificationError("境外生产企业注册已失效")
        qualification = self._qualification(batch.qualification_id)
        if qualification.status is not RegistrationStatus.ACTIVE:
            raise QualificationError("产品资格已失效")
        confirmed = self._latest_confirmed(batch.batch_id)
        sample_by_id = self._samples
        for decision_line in parsed:
            line = batch.lines[decision_line.spec]
            if line.detained > 0:
                raise ConflictError("整票扣留与局部放行冲突，须先解除扣留")
            if decision_line.quantity > line.pending:
                raise ConflictError("放行数量超出在控货量")
            for conclusion in confirmed:
                if decision_line.spec in conclusion.failed_specs:
                    raise ConflictError("存在不合格结论，不得放行")
            if batch.category in rules.high_risk_categories and not any(
                conclusion.passed and sample_by_id[conclusion.sample_id].spec == decision_line.spec
                for conclusion in confirmed
            ):
                raise StateError("高风险品类须检测合格后方可放行")
        if batch.category in rules.cold_chain_categories:
            records = self._temperature.get(batch.container_no, ())
            if not records:
                raise StateError("缺少温控记录")
            if any(record.celsius < rules.temperature_min or record.celsius > rules.temperature_max for record in records):
                raise StateError("温控记录超出规则范围")
        for decision_line in parsed:
            batch.lines[decision_line.spec].released += decision_line.quantity

    def approve_decision(self, principal: Principal, decision_id: str, now: datetime) -> DisposalDecision:
        """退运、销毁决定须由决定人以外的海关人员批准。"""
        self._require_role(principal, Role.CUSTOMS)
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise NotFoundError(f"处置决定不存在：{decision_id}")
        if decision.decision_type not in (DecisionType.RETURN, DecisionType.DESTROY):
            raise StateError("该决定无需批准")
        if decision.approved:
            raise StateError("决定已批准")
        if principal.actor_id == decision.decided_by:
            raise PermissionDeniedError("决定人与批准人不得相同")
        decision.approvals = decision.approvals + (Approval(approver_id=principal.actor_id, approved_at=now),)
        decision.approved = True
        return decision

    def record_certificate(
        self,
        principal: Principal,
        certificate_id: str,
        decision_id: str,
        lines: tuple[tuple[str, Any], ...],
        proof: str,
        now: datetime,
    ) -> DisposalCertificate:
        """登记退运或销毁证明，数量须与决定一致；重送相同内容返回原证明。"""
        self._require_role(principal, Role.CUSTOMS, Role.EXECUTOR)
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise NotFoundError(f"处置决定不存在：{decision_id}")
        if decision.decision_type not in (DecisionType.RETURN, DecisionType.DESTROY):
            raise StateError("仅退运或销毁决定需要证明")
        parsed = tuple(
            sorted(
                (DecisionLine(spec=spec, quantity=self._qty(qty)) for spec, qty in lines),
                key=lambda line: line.spec,
            )
        )
        existing = self._certificates.get(certificate_id)
        if existing is not None:
            if existing.decision_id == decision_id and existing.lines == parsed and existing.proof == proof:
                return existing
            raise ConflictError("证明编号相同但内容不同")
        if not decision.approved:
            raise StateError("处置决定未批准")
        if {line.spec: line.quantity for line in parsed} != {line.spec: line.quantity for line in decision.lines}:
            raise ConflictError("证明数量与处置决定不一致")
        certificate = DisposalCertificate(
            certificate_id=certificate_id,
            decision_id=decision_id,
            executor_id=principal.actor_id,
            lines=parsed,
            proof=proof,
            issued_at=now,
        )
        self._certificates[certificate_id] = certificate
        decision.executed = True
        return certificate

    # -- 渠道回执 ------------------------------------------------------------

    def submit_channel_receipt(
        self,
        principal: Principal,
        channel: str,
        receipt_no: str,
        batch_ids: tuple[str, ...],
        payload: dict[str, Any],
        now: datetime,
    ) -> ChannelReceipt:
        """接收渠道回执：重送相同内容返回原结果，编号相同内容不同则暂停相关批次。"""
        self._require_role(principal, Role.CUSTOMS, Role.EXECUTOR)
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        key = (channel, receipt_no)
        existing = self._receipts.get(key)
        if existing is not None:
            if existing.fingerprint == fingerprint:
                return existing
            for batch_id in sorted(set(existing.batch_ids) | set(batch_ids)):
                batch = self._batches.get(batch_id)
                if batch is not None:
                    batch.suspended = True
                    batch.suspend_reason = f"渠道回执{channel}/{receipt_no}内容不一致"
            raise ReceiptConflictError(f"渠道回执编号相同但内容不同，已暂停相关批次：{channel}/{receipt_no}")
        receipt = ChannelReceipt(
            channel=channel,
            receipt_no=receipt_no,
            fingerprint=fingerprint,
            batch_ids=tuple(batch_ids),
            received_at=now,
            result="已受理",
        )
        self._receipts[key] = receipt
        return receipt

    def resume_batch(self, principal: Principal, batch_id: str, now: datetime) -> GoodsBatch:
        """海关核查后解除批次暂停。"""
        self._require_role(principal, Role.CUSTOMS)
        batch = self._batch(batch_id)
        batch.suspended = False
        batch.suspend_reason = ""
        return batch

    # -- 企业整改 ------------------------------------------------------------

    def open_rectification(
        self,
        principal: Principal,
        rectification_id: str,
        registration_id: str,
        issue: str,
        now: datetime,
    ) -> Rectification:
        """对境外企业开具整改要求，期限按现行规则计算。"""
        self._require_role(principal, Role.CUSTOMS)
        if rectification_id in self._rectifications:
            raise ConflictError(f"整改编号已存在：{rectification_id}")
        self._registration(registration_id)
        rules = self._rules_now(now)
        rectification = Rectification(
            rectification_id=rectification_id,
            registration_id=registration_id,
            issue=issue,
            opened_at=now,
            deadline=now + timedelta(days=rules.rectification_deadline_days),
        )
        self._rectifications[rectification_id] = rectification
        return rectification

    def submit_rectification(
        self,
        principal: Principal,
        rectification_id: str,
        evidence: str,
        now: datetime,
    ) -> Rectification:
        """涉事境外企业提交整改材料，生成整改复核任务。"""
        rectification = self._rectifications.get(rectification_id)
        if rectification is None:
            raise NotFoundError(f"整改不存在：{rectification_id}")
        registration = self._registration(rectification.registration_id)
        if principal.role is not Role.ENTERPRISE or principal.org_id != registration.enterprise_id:
            raise PermissionDeniedError("只能由涉事境外企业提交整改材料")
        if rectification.status is not RectificationStatus.OPEN:
            raise StateError("整改不在待提交状态")
        rectification.evidence = evidence
        rectification.status = RectificationStatus.SUBMITTED
        task = InspectionTask(
            task_id=f"RREV-{rectification_id}",
            kind=TaskKind.RECTIFICATION_REVIEW,
            ref_id=rectification_id,
            assignee=Role.CUSTOMS.value,
            created_at=now,
            due_at=rectification.deadline,
        )
        self._tasks[task.task_id] = task
        return rectification

    def review_rectification(
        self,
        principal: Principal,
        rectification_id: str,
        passed: bool,
        now: datetime,
    ) -> Rectification:
        """海关复核整改；复核未通过则暂停企业注册。"""
        self._require_role(principal, Role.CUSTOMS)
        rectification = self._rectifications.get(rectification_id)
        if rectification is None:
            raise NotFoundError(f"整改不存在：{rectification_id}")
        if rectification.status is not RectificationStatus.SUBMITTED:
            raise StateError("整改尚未提交")
        rectification.status = RectificationStatus.PASSED if passed else RectificationStatus.FAILED
        rectification.reviewed_by = principal.actor_id
        task = self._tasks.get(f"RREV-{rectification_id}")
        if task is not None:
            task.status = TaskStatus.DONE
            task.result = "通过" if passed else "不通过"
        if not passed:
            self.change_registration(
                principal,
                rectification.registration_id,
                now,
                status=RegistrationStatus.SUSPENDED,
                reason=f"整改复核未通过：{rectification_id}",
            )
        return rectification

    # -- 访问控制视图 ----------------------------------------------------------

    def _batch_sections(self, batch: GoodsBatch) -> dict[str, Any]:
        samples = [s for s in self._samples.values() if s.batch_id == batch.batch_id]
        sample_ids = {s.sample_id for s in samples}
        conclusions = [c for c in self._conclusions.values() if c.sample_id in sample_ids]
        decisions = [d for d in self._decisions.values() if d.batch_id == batch.batch_id]
        decision_ids = {d.decision_id for d in decisions}
        certificates = [c for c in self._certificates.values() if c.decision_id in decision_ids]
        return {
            "批次": {
                "batch_id": batch.batch_id,
                "container_no": batch.container_no,
                "manifest_id": batch.manifest_id,
                "importer_id": batch.importer_id,
                "category": batch.category,
                "hs_code": batch.hs_code,
                "status": self._status_of(batch),
                "lines": dict(batch.lines),
            },
            "资格": {
                "registration": self._registration(batch.registration_id),
                "qualification": self._qualification(batch.qualification_id),
            },
            "审单": {
                "review_passed": batch.review_passed,
                "task": self._tasks.get(f"REV-{batch.batch_id}"),
            },
            "温控": list(self._temperature.get(batch.container_no, ())),
            "样品": samples,
            "检测": conclusions,
            "决定": decisions,
            "证明": certificates,
        }

    def view_batch(self, principal: Principal, batch_id: str) -> dict[str, Any]:
        """按角色查看批次材料：企业只看自己，协查按履职范围开放。"""
        batch = self._batch(batch_id)
        sections = self._batch_sections(batch)
        if principal.role is Role.CUSTOMS:
            return sections
        if principal.role is Role.ENTERPRISE:
            if self._owns_batch(principal, batch):
                return sections
            raise PermissionDeniedError("企业只能查看自己的材料")
        if principal.role is Role.LAB:
            own_samples = [
                s for s in sections["样品"] if principal.org_id in (s.lab_id, s.active_lab_id)
            ]
            if not own_samples:
                raise PermissionDeniedError("实验室只能查看承检样品")
            own_ids = {s.sample_id for s in own_samples}
            return {
                "批次": sections["批次"],
                "样品": own_samples,
                "检测": [c for c in sections["检测"] if c.sample_id in own_ids],
            }
        if principal.role is Role.COLLABORATOR:
            allowed: dict[str, Any] = {}
            if "处置协查" in principal.duties:
                allowed["批次"] = sections["批次"]
                allowed["决定"] = sections["决定"]
                allowed["证明"] = sections["证明"]
            if "风险预警" in principal.duties:
                allowed.setdefault("批次", sections["批次"])
                allowed["样品"] = sections["样品"]
                allowed["检测"] = sections["检测"]
            if not allowed:
                raise PermissionDeniedError("协查履职范围未覆盖该批次材料")
            return allowed
        raise PermissionDeniedError("无权查看该批次材料")

    def view_sample(self, principal: Principal, sample_id: str) -> Sample:
        sample = self._sample(sample_id)
        if principal.role is Role.CUSTOMS:
            return sample
        if principal.role is Role.LAB and principal.org_id in (sample.lab_id, sample.active_lab_id):
            return sample
        if principal.role is Role.ENTERPRISE and self._owns_batch(principal, self._batch(sample.batch_id)):
            return sample
        raise PermissionDeniedError("无权查看该样品")

    def batch_status(self, batch_id: str) -> str:
        return self._status_of(self._batch(batch_id))

    # -- 恢复与溯源 ------------------------------------------------------------

    def recover(self, now: datetime) -> RecoveryPlan:
        """服务恢复后继续办理：超期样品、待处置货物和整改复核任务。"""
        overdue_samples = sorted(
            sample.sample_id
            for sample in self._samples.values()
            if sample.status is SampleStatus.IN_LAB and sample.lab_deadline < now
        )
        undecided = {
            batch.batch_id
            for batch in self._batches.values()
            if any(line.detained > 0 for line in batch.lines.values())
        }
        uncertified = {
            decision.batch_id
            for decision in self._decisions.values()
            if decision.decision_type in (DecisionType.RETURN, DecisionType.DESTROY) and not decision.executed
        }
        reviews = sorted(
            rectification.rectification_id
            for rectification in self._rectifications.values()
            if rectification.status is RectificationStatus.SUBMITTED
            or (rectification.status is RectificationStatus.OPEN and rectification.deadline < now)
        )
        return RecoveryPlan(
            overdue_samples=tuple(overdue_samples),
            pending_disposal_batches=tuple(sorted(undecided | uncertified)),
            pending_rectification_reviews=tuple(reviews),
        )

    def explain_decision(self, principal: Principal, decision_id: str) -> dict[str, Any]:
        """还原决定形成时采用的资格、规则、检测和批准依据。"""
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise NotFoundError(f"处置决定不存在：{decision_id}")
        batch = self._batch(decision.batch_id)
        if principal.role is Role.CUSTOMS:
            pass
        elif principal.role is Role.COLLABORATOR and "处置协查" in principal.duties:
            pass
        elif principal.role is Role.ENTERPRISE and self._owns_batch(principal, batch):
            pass
        else:
            raise PermissionDeniedError("无权还原该决定依据")
        basis = decision.basis
        return {
            "decision": decision,
            "batch_status": self._status_of(batch),
            "registration": self.registration_at(batch.registration_id, basis.registration_version),
            "qualification": self.qualification_at(batch.qualification_id, basis.qualification_version),
            "rules": self.rules_version(basis.rule_version),
            "conclusions": [self._conclusions[cid] for cid in basis.conclusion_ids],
            "temperature_records": [
                record
                for record in self._temperature.get(batch.container_no, ())
                if record.record_id in basis.temperature_record_ids
            ],
            "approvals": decision.approvals,
        }

    # -- 快照与恢复 ------------------------------------------------------------

    def to_snapshot(self) -> dict[str, Any]:
        """导出全部状态，可 JSON 序列化。"""
        return {
            "domain": DOMAIN,
            "stores": {name: _encode(getattr(self, name)) for name in _STORE_TYPES},
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> InspectionService:
        """从快照恢复服务，恢复后可继续办理存量业务。"""
        if not isinstance(data, dict) or data.get("domain") != DOMAIN:
            raise ValueError("快照领域标识不一致")
        stores = data.get("stores")
        if not isinstance(stores, dict):
            raise ValueError("快照内容不完整")
        service = cls()
        for name, expected in _STORE_TYPES.items():
            setattr(service, name, _decode(expected, stores[name]))
        return service


# ---------------------------------------------------------------------------
# 快照编解码
# ---------------------------------------------------------------------------

_ENTITY_TYPES = (
    RegistrationVersion,
    QualificationVersion,
    Manifest,
    Container,
    TemperatureRecord,
    LineState,
    GoodsBatch,
    RiskRuleSet,
    InspectionTask,
    SamplePortion,
    Sample,
    TestItem,
    TestConclusion,
    DecisionLine,
    Approval,
    DecisionBasis,
    DisposalDecision,
    DisposalCertificate,
    ChannelReceipt,
    Rectification,
)

_STORE_TYPES: dict[str, Any] = {
    "_registrations": dict[str, list[RegistrationVersion]],
    "_qualifications": dict[str, list[QualificationVersion]],
    "_manifests": dict[str, Manifest],
    "_containers": dict[str, Container],
    "_temperature": dict[str, list[TemperatureRecord]],
    "_batches": dict[str, GoodsBatch],
    "_rules": dict[int, RiskRuleSet],
    "_tasks": dict[str, InspectionTask],
    "_samples": dict[str, Sample],
    "_conclusions": dict[str, TestConclusion],
    "_decisions": dict[str, DisposalDecision],
    "_certificates": dict[str, DisposalCertificate],
    "_receipts": dict[tuple[str, str], ChannelReceipt],
    "_rectifications": dict[str, Rectification],
}


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return {"@enum": type(value).__name__, "name": value.name}
    if isinstance(value, Decimal):
        return {"@decimal": str(value)}
    if isinstance(value, datetime):
        return {"@datetime": value.isoformat()}
    if isinstance(value, date):
        return {"@date": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "@entity": type(value).__name__,
            "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, dict):
        return {"@dict": [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple, frozenset)):
        return [_encode(item) for item in value]
    raise TypeError(f"无法序列化：{type(value)!r}")


def _decode(expected: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(expected)
    if origin in (Union, UnionType):
        for arg in get_args(expected):
            if arg is not type(None):
                return _decode(arg, value)
        raise TypeError(f"无法解码：{expected!r}")
    if origin is dict:
        key_type, value_type = get_args(expected)
        return {_decode(key_type, k): _decode(value_type, v) for k, v in value["@dict"]}
    if origin is tuple:
        item_type = get_args(expected)[0]
        return tuple(_decode(item_type, item) for item in value)
    if origin is list:
        (item_type,) = get_args(expected)
        return [_decode(item_type, item) for item in value]
    if isinstance(expected, type):
        if issubclass(expected, Enum):
            return expected[value["name"]]
        if expected is Decimal:
            return Decimal(value["@decimal"])
        if expected is datetime:
            return datetime.fromisoformat(value["@datetime"])
        if expected is date:
            return date.fromisoformat(value["@date"])
        if is_dataclass(expected):
            hints = get_type_hints(expected)
            encoded = value["fields"]
            return expected(
                **{f.name: _decode(hints[f.name], encoded[f.name]) for f in fields(expected) if f.name in encoded}
            )
    return value
