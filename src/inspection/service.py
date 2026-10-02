"""口岸食品准入查验服务。

覆盖：境外企业与产品资格（版本化）、审单规则（换版只影响之后的决定）、
舱单箱货与货批拆分、温控记录、查验任务、样品链（抽样/拆分/复检守恒）、
检测结论、扣留/放行/退运/销毁决定（只增不改并固化依据快照）、
处置证明、企业整改、渠道回执幂等与异文暂停、按履职范围授权、
重启恢复与决定依据还原。
"""

from __future__ import annotations

from datetime import datetime as _datetime
from typing import Any, Callable

from .auth import AccessPolicy, Role
from .errors import (
    ConflictError,
    ImmutableRecordError,
    ReceiptConflict,
    ValidationError,
)
from .models import (
    CustodyAction,
    DecisionKind,
    DecisionScope,
    EnterpriseStatus,
    LotStatus,
    RectificationStatus,
    SamplePurpose,
    SampleStatus,
    TaskStatus,
    TestConclusion,
    is_overdue,
    now_iso,
    stable_hash,
)
from .store import Store


class InspectionService:
    def __init__(self, store: Store, clock: Callable[[], str] = now_iso) -> None:
        self.store = store
        self.now = clock

    # 编号 ----------------------------------------------------------------
    def _seq(self, prefix: str) -> str:
        return f"{prefix}{self.store.next_seq(prefix):04d}"

    def _lot(self, lot_no: str) -> dict[str, Any]:
        return self.store.require("lots", lot_no, "货批")

    # 规则换版（版本只增） -------------------------------------------------
    def publish_rule_set(
        self,
        rule_set_no: str,
        name: str,
        rules: list[dict[str, Any]],
        policy: AccessPolicy,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        policy.require("rule.publish")
        if not rules:
            raise ValidationError("审单规则不能为空")
        history = self.store.table("rule_sets").setdefault(rule_set_no, [])
        version = len(history) + 1
        record = {
            "rule_set_no": rule_set_no,
            "name": name,
            "version": version,
            "rules": rules,
            "effective_at": effective_at or self.now(),
            "published_at": self.now(),
            "published_by": policy.principal_id,
            "content_hash": stable_hash(rules),
        }
        history.append(record)
        return record

    def rule_set_effective(self, rule_set_no: str, at: str | None = None) -> dict[str, Any]:
        """取 at 时点有效版本；换版不追溯影响此前的决定。"""
        moment = at or self.now()
        history = self.store.table("rule_sets").get(rule_set_no)
        if not history:
            raise ValidationError(f"规则集{rule_set_no}不存在")
        effective = [r for r in history if r["effective_at"] <= moment]
        if not effective:
            raise ValidationError(f"规则集{rule_set_no}在{moment}尚未生效")
        return effective[-1]

    def latest_rule_set(self, at: str | None = None) -> dict[str, Any]:
        histories = self.store.list("rule_sets")
        if not histories:
            raise ValidationError("尚无生效审单规则")
        latest_header = max((h[-1] for h in histories), key=lambda r: r["published_at"])
        return self.rule_set_effective(latest_header["rule_set_no"], at)

    # 境外企业注册（版本化，资格状态随版本变化） ----------------------------
    def register_enterprise(
        self,
        enterprise_no: str,
        name: str,
        product_codes: list[str],
        policy: AccessPolicy,
        status: EnterpriseStatus = EnterpriseStatus.REGISTERED,
        valid_from: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        policy.require("enterprise.register")
        if not product_codes:
            raise ValidationError("企业获准产品范围不能为空")
        history = self.store.table("registrations").setdefault(enterprise_no, [])
        record = {
            "enterprise_no": enterprise_no,
            "name": name,
            "version": len(history) + 1,
            "status": status.value,
            "product_codes": list(product_codes),
            "valid_from": valid_from or self.now(),
            "note": note,
            "registered_at": self.now(),
        }
        history.append(record)
        return record

    def registration_effective(self, enterprise_no: str, at: str | None = None) -> dict[str, Any]:
        moment = at or self.now()
        history = self.store.table("registrations").get(enterprise_no)
        if not history:
            raise ValidationError(f"境外企业{enterprise_no}未注册")
        effective = [r for r in history if r["valid_from"] <= moment]
        if not effective:
            raise ValidationError(f"境外企业{enterprise_no}在{moment}尚未获准注册")
        return effective[-1]

    # 舱单、箱货、温控 -----------------------------------------------------
    def submit_manifest(
        self,
        manifest_no: str,
        containers: list[dict[str, Any]],
        policy: AccessPolicy,
    ) -> dict[str, Any]:
        policy.require("document.review")
        if not containers:
            raise ValidationError("舱单箱货信息不能为空")
        if self.store.get("manifests", manifest_no):
            raise ValidationError(f"舱单{manifest_no}已存在")
        record = {
            "manifest_no": manifest_no,
            "submitted_at": self.now(),
            "containers": [
                {
                    "container_no": c["container_no"],
                    "seal_no": c.get("seal_no", ""),
                    "temperature_log": list(c.get("temperature_log", [])),
                }
                for c in containers
            ],
        }
        self.store.put("manifests", manifest_no, record)
        return record

    def attach_temperature_log(
        self, manifest_no: str, container_no: str, entries: list[dict[str, Any]], policy: AccessPolicy
    ) -> None:
        policy.require("document.review")
        manifest = self.store.require("manifests", manifest_no, "舱单")
        for container in manifest["containers"]:
            if container["container_no"] == container_no:
                container["temperature_log"].extend(entries)
                return
        raise ValidationError(f"集装箱{container_no}不在舱单{manifest_no}内")

    def declare_lot(
        self,
        lot_no: str,
        manifest_no: str,
        container_no: str,
        importer_no: str,
        producer_no: str,
        product_code: str,
        spec: str,
        quantity: float,
        unit: str,
        doc_nos: list[str],
        policy: AccessPolicy,
    ) -> dict[str, Any]:
        policy.require("document.review")
        manifest = self.store.require("manifests", manifest_no, "舱单")
        if not any(c["container_no"] == container_no for c in manifest["containers"]):
            raise ValidationError(f"集装箱{container_no}与舱单不符")
        if quantity <= 0 or not doc_nos:
            raise ValidationError("货批数量与批次单证不能为空")
        if self.store.get("lots", lot_no):
            raise ValidationError(f"货批{lot_no}已存在")
        record = {
            "lot_no": lot_no,
            "manifest_no": manifest_no,
            "container_no": container_no,
            "importer_no": importer_no,
            "producer_no": producer_no,
            "product_code": product_code,
            "spec": spec,
            "declared_qty": float(quantity),
            "remaining_qty": float(quantity),
            "consumed_sample_qty": 0.0,
            "unit": unit,
            "doc_nos": list(doc_nos),
            "parent_lot_no": None,
            "root_lot_no": lot_no,
            "status": LotStatus.DECLARED.value,
            "created_at": self.now(),
        }
        self.store.put("lots", lot_no, record)
        return record

    def split_lot(
        self, parent_lot_no: str, specs: list[dict[str, Any]], policy: AccessPolicy
    ) -> list[dict[str, Any]]:
        """同一集装箱内按规格拆成多个货批；子批货量之和必须等于母批。"""
        policy.require("inspection.create")
        parent = self._lot(parent_lot_no)
        splittable = {LotStatus.DECLARED.value, LotStatus.DOC_REVIEWED.value}
        if parent["status"] not in splittable:
            raise ConflictError(
                f"货批{parent_lot_no}状态为{parent['status']}，须在查验或扣留前按规格拆分"
            )
        total = round(sum(float(s["quantity"]) for s in specs), 6)
        if not specs or total != round(parent["remaining_qty"], 6):
            raise ValidationError(
                f"拆分货量之和{total}必须等于母批可拆货量{parent['remaining_qty']}"
            )
        if len({s["spec"] for s in specs}) != len(specs):
            raise ValidationError("拆分规格不得重复")
        children: list[dict[str, Any]] = []
        for spec in specs:
            child_no = self._seq("LOT-")
            child = {
                "lot_no": child_no,
                "manifest_no": parent["manifest_no"],
                "container_no": parent["container_no"],
                "importer_no": parent["importer_no"],
                "producer_no": parent["producer_no"],
                "product_code": parent["product_code"],
                "spec": spec["spec"],
                "declared_qty": float(spec["quantity"]),
                "remaining_qty": float(spec["quantity"]),
                "consumed_sample_qty": 0.0,
                "unit": parent["unit"],
                "doc_nos": list(parent["doc_nos"]),
                "parent_lot_no": parent_lot_no,
                "root_lot_no": parent["root_lot_no"],
                "status": LotStatus.DECLARED.value,
                "created_at": self.now(),
            }
            self.store.put("lots", child_no, child)
            children.append(child)
        parent["remaining_qty"] = 0.0
        parent["status"] = LotStatus.SPLIT.value
        parent["child_lot_nos"] = [c["lot_no"] for c in children]
        return children

    # 审单 ----------------------------------------------------------------
    def review_documents(
        self,
        lot_no: str,
        policy: AccessPolicy,
        rule_set_no: str | None = None,
        findings: list[str] | None = None,
    ) -> dict[str, Any]:
        policy.require("document.review")
        lot = self._lot(lot_no)
        self._guard_terminal(lot)
        if lot["status"] == LotStatus.SUSPENDED.value:
            raise ConflictError(f"货批{lot_no}已暂停，暂停核查结束前不得审单")
        if self.store.get("reviews", lot_no) is not None:
            raise ImmutableRecordError(f"货批{lot_no}已有审单记录，不得覆盖")
        findings = findings or []
        if rule_set_no is None:
            rule = self.latest_rule_set()
        else:
            rule = self.rule_set_effective(rule_set_no)
        record = {
            "lot_no": lot_no,
            "rule_set_no": rule["rule_set_no"],
            "rule_version": rule["version"],
            "rule_hash": rule["content_hash"],
            "rules_snapshot": rule["rules"],
            "findings": findings,
            "passed": not findings,
            "reviewer": policy.principal_id,
            "reviewed_at": self.now(),
        }
        self.store.put("reviews", lot_no, record)
        if findings:
            # 实质性不符先扣留该货批；集装箱级整票扣留走 detain_container
            self._issue_detain(
                [lot_no], DecisionScope.LOT, "审单发现不符：" + "；".join(findings), policy
            )
        else:
            lot["status"] = (
                LotStatus.DETAINED.value
                if self._container_detain_covers(lot)
                else LotStatus.DOC_REVIEWED.value
            )
        return record

    # 查验任务 -------------------------------------------------------------
    def create_task(
        self,
        lot_no: str,
        kind: str,
        assignee: str,
        due_at: str,
        policy: AccessPolicy,
    ) -> dict[str, Any]:
        policy.require("inspection.create")
        self._lot(lot_no)
        task = {
            "task_no": self._seq("TASK-"),
            "lot_no": lot_no,
            "kind": kind,
            "assignee": assignee,
            "due_at": due_at,
            "status": TaskStatus.PENDING.value,
            "created_at": self.now(),
            "finished_at": None,
        }
        self.store.put("tasks", task["task_no"], task)
        lot = self._lot(lot_no)
        if lot["status"] == LotStatus.DOC_REVIEWED.value:
            lot["status"] = LotStatus.INSPECTING.value
        return task

    def complete_task(self, task_no: str, policy: AccessPolicy) -> None:
        policy.require("inspection.create")
        task = self.store.require("tasks", task_no, "查验任务")
        task["status"] = TaskStatus.DONE.value
        task["finished_at"] = self.now()

    # 样品链 ---------------------------------------------------------------
    def _active_sample_qty(self, lot_no: str) -> float:
        return round(
            sum(
                s["qty"]
                for s in self.store.list("samples")
                if s["lot_no"] == lot_no and s["status"] not in (SampleStatus.EXHAUSTED.value, SampleStatus.DISPOSED.value)
            ),
            6,
        )

    def check_lot_balance(self, lot_no: str) -> None:
        """剩余货量 + 在链样品量 + 已耗样品量 == 申报货量。"""
        lot = self._lot(lot_no)
        total = round(
            lot["remaining_qty"] + self._active_sample_qty(lot_no) + lot["consumed_sample_qty"], 6
        )
        if total != round(lot["declared_qty"], 6):
            raise ValidationError(
                f"货批{lot_no}货量不平衡：在库{lot['remaining_qty']}+"
                f"在链样品{self._active_sample_qty(lot_no)}+已耗{lot['consumed_sample_qty']}"
                f"≠申报{lot['declared_qty']}"
            )

    def collect_samples(
        self,
        lot_no: str,
        portions: list[dict[str, Any]],
        policy: AccessPolicy,
        lab_no: str = "",
        due_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """取样人从货批抽样；抽样货量从在库货量划转，总量守恒。"""
        policy.require("sample.collect")
        lot = self._lot(lot_no)
        blocked = {
            LotStatus.SUSPENDED.value,
            LotStatus.SPLIT.value,
            LotStatus.AWAIT_DISPOSAL.value,
        }
        if lot["status"] in blocked:
            raise ConflictError(f"货批{lot_no}当前状态{lot['status']}不得抽样")
        total = round(sum(float(p["qty"]) for p in portions), 6)
        if total <= 0 or total > lot["remaining_qty"]:
            raise ValidationError(f"抽样货量{total}超过在库货量{lot['remaining_qty']}")
        created: list[dict[str, Any]] = []
        for part in portions:
            purpose = SamplePurpose(part.get("purpose", SamplePurpose.PRIMARY.value))
            sample_no = self._seq("SAM-")
            record = {
                "sample_no": sample_no,
                "lot_no": lot_no,
                "root_sample_no": sample_no,
                "parent_sample_no": None,
                "purpose": purpose.value,
                "qty": float(part["qty"]),
                "unit": lot["unit"],
                "status": SampleStatus.COLLECTED.value,
                "lab_no": lab_no,
                "due_at": due_at,
                "custody": [
                    {"action": CustodyAction.COLLECT.value, "by": policy.principal_id, "at": self.now()}
                ],
                "created_at": self.now(),
            }
            self.store.put("samples", sample_no, record)
            created.append(record)
        lot["remaining_qty"] = round(lot["remaining_qty"] - total, 6)
        lot["status"] = LotStatus.PENDING_TEST.value if lot["status"] != LotStatus.AWAIT_DISPOSAL.value else lot["status"]
        self.check_lot_balance(lot_no)
        return created

    def split_sample(
        self, sample_no: str, split_qty: float, purpose: SamplePurpose, policy: AccessPolicy
    ) -> dict[str, Any]:
        """从既有样品分出子样（复检必须由留样分出），父子样品量守恒。"""
        policy.require("sample.split")
        return self._do_split_sample(sample_no, split_qty, purpose, policy.principal_id)

    def _do_split_sample(
        self, sample_no: str, split_qty: float, purpose: SamplePurpose, actor: str
    ) -> dict[str, Any]:
        parent = self.store.require("samples", sample_no, "样品")
        if parent["status"] in (SampleStatus.EXHAUSTED.value, SampleStatus.DISPOSED.value):
            raise ConflictError(f"样品{sample_no}已终态，不得拆分")
        if split_qty <= 0 or split_qty > parent["qty"]:
            raise ValidationError(f"分出量{split_qty}超过样品量{parent['qty']}")
        child_no = self._seq("SAM-")
        child = {
            "sample_no": child_no,
            "lot_no": parent["lot_no"],
            "root_sample_no": parent["root_sample_no"],
            "parent_sample_no": sample_no,
            "purpose": purpose.value,
            "qty": float(split_qty),
            "unit": parent["unit"],
            "status": SampleStatus.COLLECTED.value,
            "lab_no": parent["lab_no"],
            "due_at": None,
            "custody": [
                {"action": CustodyAction.SPLIT.value, "by": actor, "at": self.now(),
                 "detail": f"自{sample_no}分出"}
            ],
            "created_at": self.now(),
        }
        parent["qty"] = round(parent["qty"] - split_qty, 6)
        parent["custody"].append(
            {"action": CustodyAction.SPLIT.value, "by": actor, "at": self.now(),
             "detail": f"分出{child_no} {split_qty}{parent['unit']}"}
        )
        self.store.put("samples", child_no, child)
        return child

    def request_retest(self, lot_no: str, split_qty: float, policy: AccessPolicy) -> dict[str, Any]:
        """对货批提复检：由海关批准后从在链留样中分出复检样，无留样不得复检。"""
        policy.require("decision.issue")
        retains = [
            s for s in self.store.list("samples")
            if s["lot_no"] == lot_no and s["purpose"] == SamplePurpose.RETAIN.value
            and s["status"] not in (SampleStatus.EXHAUSTED.value, SampleStatus.DISPOSED.value)
            and s["qty"] >= split_qty
        ]
        if not retains:
            raise ValidationError(f"货批{lot_no}没有足量留样，不能复检")
        parent = retains[0]
        child = self._do_split_sample(
            parent["sample_no"], split_qty, SamplePurpose.RETEST, policy.principal_id
        )
        child["custody"].append(
            {"action": "retest_approved", "by": policy.principal_id, "at": self.now()}
        )
        return child

    def handover_sample(self, sample_no: str, policy: AccessPolicy) -> None:
        policy.require("sample.handover")
        sample = self.store.require("samples", sample_no, "样品")
        sample["status"] = SampleStatus.IN_TRANSIT.value
        sample["custody"].append(
            {"action": CustodyAction.HANDOVER.value, "by": policy.principal_id, "at": self.now()}
        )

    def receive_sample(self, sample_no: str, policy: AccessPolicy) -> None:
        policy.require("sample.receive")
        sample = self.store.require("samples", sample_no, "样品")
        sample["status"] = SampleStatus.RECEIVED.value
        sample["custody"].append(
            {"action": CustodyAction.RECEIVE.value, "by": policy.principal_id, "at": self.now(),
             "lab": policy.principal_id}
        )

    def report_test(
        self,
        sample_no: str,
        conclusion: TestConclusion,
        items: list[dict[str, Any]],
        policy: AccessPolicy,
        lab_no: str = "",
        consume: bool = True,
    ) -> dict[str, Any]:
        """实验室出具检测结论；不合格或无法判定时货批进入待处置/待复检。"""
        policy.require("test.report")
        sample = self.store.require("samples", sample_no, "样品")
        if sample["status"] in (SampleStatus.EXHAUSTED.value, SampleStatus.DISPOSED.value):
            raise ConflictError(f"样品{sample_no}已终态")
        report = {
            "report_no": self._seq("RPT-"),
            "lot_no": sample["lot_no"],
            "sample_no": sample_no,
            "conclusion": conclusion.value,
            "items": items,
            "lab_no": lab_no or policy.principal_id,
            "tester": policy.principal_id,
            "issued_at": self.now(),
        }
        self.store.put("reports", report["report_no"], report)
        sample["status"] = SampleStatus.EXHAUSTED.value if consume else SampleStatus.RETAINED.value
        if consume:
            lot = self._lot(sample["lot_no"])
            lot["consumed_sample_qty"] = round(lot["consumed_sample_qty"] + sample["qty"], 6)
        lot = self._lot(sample["lot_no"])
        if conclusion == TestConclusion.UNQUALIFIED:
            lot["status"] = LotStatus.AWAIT_DISPOSAL.value
        elif conclusion == TestConclusion.INCONCLUSIVE:
            lot["status"] = LotStatus.PENDING_TEST.value
        # 合格不自动放行：最终合格必须由海关批准
        self.check_lot_balance(sample["lot_no"])
        return report

    # 决定（只增不改） ------------------------------------------------------
    def _issue_detain(
        self, lot_nos: list[str], scope: DecisionScope, reason: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        decision = self._write_decision(DecisionKind.DETAIN, lot_nos, scope, reason, policy)
        decision["prior_statuses"] = {no: self._lot(no)["status"] for no in lot_nos}
        for lot_no in lot_nos:
            lot = self._lot(lot_no)
            if lot["status"] != LotStatus.AWAIT_DISPOSAL.value:
                lot["status"] = LotStatus.DETAINED.value
        return decision

    def detain_container(
        self, manifest_no: str, container_no: str, reason: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        """整票扣留：扣留集装箱内全部在途货批。"""
        policy.require("decision.issue")
        lot_nos = [
            l["lot_no"]
            for l in self.store.list("lots")
            if l["manifest_no"] == manifest_no
            and l["container_no"] == container_no
            and l["status"]
            not in (LotStatus.RELEASED.value, LotStatus.RETURNED.value, LotStatus.DESTROYED.value,
                    LotStatus.SPLIT.value)
        ]
        if not lot_nos:
            raise ValidationError("集装箱内没有可扣留的货批")
        return self._issue_detain(lot_nos, DecisionScope.CONTAINER, reason, policy)

    def narrow_detention(
        self, detain_decision_no: str, held_lot_nos: list[str], reason: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        """一项不合格只影响部分规格：缩小原整票扣留范围，其余货批可放行。"""
        policy.require("decision.issue")
        original = self.store.require("decisions", detain_decision_no, "扣留决定")
        if original["kind"] != DecisionKind.DETAIN.value or original["scope"] != DecisionScope.CONTAINER.value:
            raise ValidationError("只能针对集装箱级整票扣留决定缩小范围")
        outside = set(held_lot_nos) - set(original["lot_nos"])
        if outside:
            raise ValidationError(f"缩小范围包含不属于原扣留的货批：{outside}")
        decision = self._write_decision(
            DecisionKind.NARROW_DETENTION,
            original["lot_nos"],
            DecisionScope.CONTAINER,
            reason,
            policy,
            extra={"supersedes": detain_decision_no, "held_lot_nos": list(held_lot_nos)},
        )
        for lot_no in original["lot_nos"]:
            lot = self._lot(lot_no)
            if lot_no in held_lot_nos:
                lot["status"] = LotStatus.DETAINED.value
            else:
                # 恢复到整票扣留前的在途状态，继续后续审单/查验
                lot["status"] = original.get(
                    "prior_statuses", {}
                ).get(lot_no, LotStatus.DECLARED.value)
        return decision

    def _container_detain_covers(self, lot: dict[str, Any]) -> dict[str, Any] | None:
        """返回仍覆盖该货批的整票扣留决定（未被缩小范围排除的）。"""
        for d in self.store.list("decisions"):
            if (
                d["kind"] == DecisionKind.DETAIN.value
                and d["scope"] == DecisionScope.CONTAINER.value
                and d.get("manifest_no") == lot["manifest_no"]
                and d.get("container_no") == lot["container_no"]
                and lot["lot_no"] in d["lot_nos"]
            ):
                # 查看之后是否有缩小范围决定
                narrowed = [
                    n for n in self.store.list("decisions")
                    if n["kind"] == DecisionKind.NARROW_DETENTION.value
                    and n.get("supersedes") == d["decision_no"]
                    and n["issued_at"] >= d["issued_at"]
                ]
                if not narrowed or lot["lot_no"] in narrowed[-1]["held_lot_nos"]:
                    return d
        return None

    def _latest_conclusion(self, lot_no: str) -> TestConclusion | None:
        reports = [r for r in self.store.list("reports") if r["lot_no"] == lot_no]
        if not reports:
            return None
        return TestConclusion(sorted(reports, key=lambda r: r["issued_at"])[-1]["conclusion"])

    def _eligibility(self, lot: dict[str, Any], at: str) -> dict[str, Any]:
        """决定时点有效的企业与产品资格。"""
        registration = self.registration_effective(lot["producer_no"], at)
        if registration["status"] != EnterpriseStatus.REGISTERED.value:
            raise ConflictError(
                f"境外企业{lot['producer_no']}当前状态{registration['status']}，产品不准入境"
            )
        if lot["product_code"] not in registration["product_codes"]:
            raise ConflictError(f"产品{lot['product_code']}不在企业{lot['producer_no']}获准范围内")
        return registration

    def _build_basis(self, lot: dict[str, Any]) -> dict[str, Any]:
        """固化决定当时采用的资格、规则、检测、温控与批准依据。"""
        at = self.now()
        registration = self.registration_effective(lot["producer_no"], at)
        review = self.store.get("reviews", lot["lot_no"])
        manifest = self.store.get("manifests", lot["manifest_no"])
        container = next(
            (c for c in (manifest["containers"] if manifest else []) if c["container_no"] == lot["container_no"]),
            None,
        )
        samples = [s for s in self.store.list("samples") if s["lot_no"] == lot["lot_no"]]
        reports = [r for r in self.store.list("reports") if r["lot_no"] == lot["lot_no"]]
        prior_decisions = [
            {
                "decision_no": d["decision_no"],
                "kind": d["kind"],
                "scope": d["scope"],
                "issued_at": d["issued_at"],
                "issuer": d["issuer"],
                "reason": d["reason"],
            }
            for d in self.store.list("decisions")
            if lot["lot_no"] in d.get("lot_nos", [])
        ]
        basis = {
            "captured_at": at,
            "lot": dict(lot),
            "registration": dict(registration),
            "review": dict(review) if review else None,
            "rule": (
                {
                    "rule_set_no": review["rule_set_no"],
                    "version": review["rule_version"],
                    "content_hash": review["rule_hash"],
                    "rules": review["rules_snapshot"],
                }
                if review
                else None
            ),
            "manifest": {"manifest_no": lot["manifest_no"]},
            "container": dict(container) if container else None,
            "docs": list(lot["doc_nos"]),
            "samples": [dict(s) for s in samples],
            "reports": [dict(r) for r in reports],
            "prior_decisions": prior_decisions,
        }
        basis["basis_hash"] = stable_hash(basis)
        return basis

    def _write_decision(
        self,
        kind: DecisionKind,
        lot_nos: list[str],
        scope: DecisionScope,
        reason: str,
        policy: AccessPolicy,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        first = self._lot(lot_nos[0])
        decision: dict[str, Any] = {
            "decision_no": self._seq("DEC-"),
            "kind": kind.value,
            "scope": scope.value,
            "lot_nos": list(lot_nos),
            "manifest_no": first["manifest_no"],
            "container_no": first["container_no"],
            "reason": reason,
            "issuer": policy.principal_id,
            "issued_at": self.now(),
        }
        if kind in (DecisionKind.RELEASE, DecisionKind.RETURN, DecisionKind.DESTROY):
            decision["basis"] = {}  # 逐批填充
        if extra:
            decision.update(extra)
        self.store.put("decisions", decision["decision_no"], decision)
        return decision

    def _guard_terminal(self, lot: dict[str, Any]) -> None:
        if lot["status"] in (LotStatus.RELEASED.value, LotStatus.RETURNED.value, LotStatus.DESTROYED.value):
            raise ImmutableRecordError(
                f"货批{lot['lot_no']}已{lot['status']}，该终局事实不可覆盖"
            )

    def release_lots(self, lot_nos: list[str], reason: str, policy: AccessPolicy) -> dict[str, Any]:
        """海关批准放行；取样人不能独自确认最终合格。"""
        policy.require("decision.issue")
        if not lot_nos:
            raise ValidationError("放行货批不能为空")
        # 先完成全部货批校验并固化依据，任一不满足则不产生决定
        basis_by_lot: dict[str, Any] = {}
        for lot_no in lot_nos:
            lot = self._lot(lot_no)
            self._guard_terminal(lot)
            if lot["status"] == LotStatus.SUSPENDED.value:
                raise ConflictError(f"货批{lot_no}已暂停，不得放行")
            if self._container_detain_covers(lot):
                raise ConflictError(f"货批{lot_no}仍在整票扣留范围内，须先缩小扣留范围或解除扣留")
            self._eligibility(lot, self.now())
            review = self.store.get("reviews", lot_no)
            if review is None or not review["passed"]:
                raise ConflictError(f"货批{lot_no}审单未通过，不得放行")
            conclusion = self._latest_conclusion(lot_no)
            if conclusion is None:
                raise ConflictError(f"货批{lot_no}尚无检测结论，海关不得批准放行")
            if conclusion != TestConclusion.QUALIFIED:
                raise ConflictError(f"货批{lot_no}最新检测结论为{conclusion.value}，不得放行")
            self.check_lot_balance(lot_no)
            basis_by_lot[lot_no] = self._build_basis(lot)
        decision = self._write_decision(DecisionKind.RELEASE, lot_nos, DecisionScope.LOT, reason, policy)
        decision["basis"] = basis_by_lot
        for lot_no in lot_nos:
            self._lot(lot_no)["status"] = LotStatus.RELEASED.value
        return decision

    def dispose_lots(
        self, kind: DecisionKind, lot_nos: list[str], reason: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        """退运/销毁终局决定；规则换版不影响已形成的事实。"""
        policy.require("decision.issue")
        if kind not in (DecisionKind.RETURN, DecisionKind.DESTROY):
            raise ValidationError("处置决定只能是退运或销毁")
        if not lot_nos:
            raise ValidationError("处置货批不能为空")
        disposable = {LotStatus.DETAINED.value, LotStatus.AWAIT_DISPOSAL.value}
        basis_by_lot: dict[str, Any] = {}
        for lot_no in lot_nos:
            lot = self._lot(lot_no)
            self._guard_terminal(lot)
            if lot["status"] == LotStatus.SUSPENDED.value:
                raise ConflictError(f"货批{lot_no}已暂停，须先核查")
            if lot["status"] not in disposable:
                raise ConflictError(f"货批{lot_no}状态为{lot['status']}，尚不构成退运/销毁条件")
            self.check_lot_balance(lot_no)
            basis_by_lot[lot_no] = self._build_basis(lot)
        decision = self._write_decision(kind, lot_nos, DecisionScope.LOT, reason, policy)
        decision["basis"] = basis_by_lot
        for lot_no in lot_nos:
            lot = self._lot(lot_no)
            lot["status"] = LotStatus.AWAIT_DISPOSAL.value
            lot["pending_decision_no"] = decision["decision_no"]
        return decision

    def execute_disposal(
        self, decision_no: str, proof_ref: str, vessel_or_method: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        """处置执行单位回填退运/销毁证明，货批进入终态。"""
        policy.require("decision.execute")
        decision = self.store.require("decisions", decision_no, "处置决定")
        if decision["kind"] not in (DecisionKind.RETURN.value, DecisionKind.DESTROY.value):
            raise ValidationError("只能对退运/销毁决定回填执行证明")
        existing = [
            c for c in self.store.list("certificates") if c["decision_no"] == decision_no
        ]
        if existing:
            raise ImmutableRecordError(f"决定{decision_no}的处置证明已存在，不可重复出具")
        kind = (
            DecisionKind.RETURN.value if decision["kind"] == DecisionKind.RETURN.value
            else DecisionKind.DESTROY.value
        )
        cert = {
            "cert_no": self._seq("CERT-"),
            "decision_no": decision_no,
            "kind": kind,
            "lot_nos": list(decision["lot_nos"]),
            "proof_ref": proof_ref,
            "vessel_or_method": vessel_or_method,
            "executed_by": policy.principal_id,
            "executed_at": self.now(),
        }
        self.store.put("certificates", cert["cert_no"], cert)
        terminal = LotStatus.RETURNED if kind == DecisionKind.RETURN.value else LotStatus.DESTROYED
        for lot_no in decision["lot_nos"]:
            self._lot(lot_no)["status"] = terminal.value
        return cert

    # 渠道回执：重送幂等、异文暂停 ------------------------------------------
    def receive_channel_receipt(
        self, message_id: str, lot_no: str, payload: dict[str, Any], policy: AccessPolicy
    ) -> dict[str, Any]:
        policy.require("channel.receive")
        self._lot(lot_no)
        payload_hash = stable_hash(payload)
        seen = self.store.get("receipts", message_id)
        if seen is not None:
            if seen["payload_hash"] == payload_hash:
                # 同一渠道回执重送：只返回原结果，不产生新记录
                return {"dedup": True, "result": seen["result"], "first_at": seen["first_at"]}
            # 编号相同但内容不同：暂停相关批次（已放行/退运/销毁的终态事实不动）
            related = self._related_lot_nos(lot_no)
            prior_statuses = {no: self._lot(no)["status"] for no in related}
            for related_lot_no in related:
                self._lot(related_lot_no)["status"] = LotStatus.SUSPENDED.value
            suspension = {
                "lot_no": lot_no,
                "related_lot_nos": related,
                "prior_statuses": prior_statuses,
                "message_id": message_id,
                "original_hash": seen["payload_hash"],
                "incoming_hash": payload_hash,
                "at": self.now(),
                "resolved_at": None,
            }
            self.store.put("suspensions", f"{message_id}:{lot_no}", suspension)
            raise ReceiptConflict(message_id, lot_no)
        record = {
            "message_id": message_id,
            "lot_no": lot_no,
            "payload_hash": payload_hash,
            "first_at": self.now(),
            "result": {"accepted": True},
        }
        self.store.put("receipts", message_id, record)
        return {"dedup": False, "result": record["result"], "first_at": record["first_at"]}

    def _related_lot_nos(self, lot_no: str) -> list[str]:
        """同箱货批与同根拆分批次一并暂停。"""
        lot = self._lot(lot_no)
        related = {lot_no}
        for other in self.store.list("lots"):
            if (
                other["manifest_no"] == lot["manifest_no"]
                and other["container_no"] == lot["container_no"]
            ) or other.get("root_lot_no") == lot.get("root_lot_no"):
                if other["status"] not in (
                    LotStatus.RELEASED.value, LotStatus.RETURNED.value, LotStatus.DESTROYED.value
                ):
                    related.add(other["lot_no"])
        return sorted(related)

    def resolve_suspension(self, message_id: str, lot_no: str, policy: AccessPolicy) -> None:
        """人工核查后恢复暂停批次到待处置状态。"""
        policy.require("decision.issue")
        suspension = self.store.require("suspensions", f"{message_id}:{lot_no}", "暂停记录")
        suspension["resolved_at"] = self.now()
        for related_lot_no in suspension["related_lot_nos"]:
            lot = self._lot(related_lot_no)
            if lot["status"] == LotStatus.SUSPENDED.value:
                lot["status"] = suspension.get(
                    "prior_statuses", {}
                ).get(related_lot_no, LotStatus.DECLARED.value)

    # 企业整改与复核 --------------------------------------------------------
    def open_rectification(
        self, lot_no: str, requirement: str, due_at: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        policy.require("decision.issue")
        self._lot(lot_no)
        record = {
            "rect_no": self._seq("RECT-"),
            "lot_no": lot_no,
            "producer_no": self._lot(lot_no)["producer_no"],
            "requirement": requirement,
            "due_at": due_at,
            "status": RectificationStatus.OPEN.value,
            "submissions": [],
            "created_at": self.now(),
            "reviewed_at": None,
            "reviewer": None,
            "note": "",
        }
        self.store.put("rectifications", record["rect_no"], record)
        return record

    def submit_rectification(
        self, rect_no: str, content: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        rect = self.store.require("rectifications", rect_no, "整改")
        lot = self._lot(rect["lot_no"])
        if policy.role not in (Role.IMPORTER, Role.OVERSEAS_PRODUCER) or policy.enterprise_no not in (
            lot["importer_no"], lot["producer_no"]
        ):
            from .errors import PermissionDenied

            raise PermissionDenied("只有当事企业可以提交整改材料")
        if rect["status"] == RectificationStatus.ACCEPTED.value:
            raise ImmutableRecordError("整改已复核通过，不可重复提交")
        rect["submissions"].append({"content": content, "at": self.now(), "by": policy.principal_id})
        rect["status"] = RectificationStatus.SUBMITTED.value
        rect["submitted_at"] = self.now()
        return rect

    def review_rectification(
        self, rect_no: str, accepted: bool, note: str, policy: AccessPolicy
    ) -> dict[str, Any]:
        policy.require("rectification.review")
        rect = self.store.require("rectifications", rect_no, "整改")
        if rect["status"] != RectificationStatus.SUBMITTED.value:
            raise ValidationError("整改材料尚未提交，不能复核")
        rect["status"] = (
            RectificationStatus.ACCEPTED.value if accepted else RectificationStatus.REJECTED.value
        )
        rect["reviewed_at"] = self.now()
        rect["reviewer"] = policy.principal_id
        rect["note"] = note
        return rect

    # 断点恢复 --------------------------------------------------------------
    def recover(self, at: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """服务恢复后继续：超期样品、待处置货物、整改复核任务、超期查验任务。"""
        moment = at or self.now()
        moment_dt = _datetime.fromisoformat(moment)
        overdue_samples = [
            {"sample_no": s["sample_no"], "lot_no": s["lot_no"], "due_at": s["due_at"],
             "status": s["status"], "lab_no": s["lab_no"]}
            for s in self.store.list("samples")
            if is_overdue(s.get("due_at"), moment_dt)
            and s["status"] not in (SampleStatus.EXHAUSTED.value, SampleStatus.DISPOSED.value)
        ]
        awaiting = [
            {"lot_no": l["lot_no"], "spec": l["spec"], "status": l["status"],
             "pending_decision_no": l.get("pending_decision_no")}
            for l in self.store.list("lots")
            if l["status"] in (LotStatus.AWAIT_DISPOSAL.value, LotStatus.DETAINED.value,
                               LotStatus.SUSPENDED.value, LotStatus.PENDING_TEST.value)
        ]
        overdue_rectifications = [
            {"rect_no": r["rect_no"], "lot_no": r["lot_no"], "status": r["status"],
             "due_at": r["due_at"]}
            for r in self.store.list("rectifications")
            if r["status"] in (RectificationStatus.OPEN.value, RectificationStatus.SUBMITTED.value)
            and is_overdue(r["due_at"], moment_dt)
        ]
        open_tasks = [
            t for t in self.store.list("tasks")
            if t["status"] != TaskStatus.DONE.value
        ]
        return {
            "overdue_samples": overdue_samples,
            "await_disposal_lots": awaiting,
            "overdue_rectifications": overdue_rectifications,
            "open_tasks": open_tasks,
            "recovered_at": moment,
        }

    # 依据还原 --------------------------------------------------------------
    def reconstruct_decision(self, decision_no: str, policy: AccessPolicy) -> dict[str, Any]:
        """从任一放行/退运/销毁决定还原当时采用的资格、规则、检测与批准依据。"""
        if not policy.can("read_all"):
            # 非全局角色按货批范围逐批校验
            decision = self.store.require("decisions", decision_no, "决定")
            for lot_no in decision["lot_nos"]:
                lot = self._lot(lot_no)
                if not policy.can_read_lot(lot_no, lot["importer_no"], lot["producer_no"]):
                    raise policy.deny_read()
        decision = self.store.require("decisions", decision_no, "决定")
        result = dict(decision)
        result["immutable"] = True
        result["basis_verification"] = self._verify_basis(decision)
        return result

    def _verify_basis(self, decision: dict[str, Any]) -> dict[str, Any]:
        """校验固化快照的完整性，并核对与当前存量记录的关系。"""
        checks: dict[str, Any] = {}
        for lot_no, basis in decision.get("basis", {}).items():
            stored_hash = basis.get("basis_hash", "")
            recomputed = stable_hash(
                {k: v for k, v in basis.items() if k != "basis_hash"}
            )
            lot_check: dict[str, Any] = {
                "lot_no": lot_no,
                "basis_hash_intact": stored_hash == recomputed,
            }
            if basis.get("rule"):
                lot_check["rule_hash_matches_snapshot"] = (
                    stable_hash(basis["rule"]["rules"]) == basis["rule"]["content_hash"]
                )
            lot_check["registration_at_issue"] = {
                "enterprise_no": basis["registration"]["enterprise_no"],
                "version": basis["registration"]["version"],
                "status": basis["registration"]["status"],
                "product_codes": basis["registration"]["product_codes"],
            }
            lot_check["rule_at_issue"] = (
                {"rule_set_no": basis["rule"]["rule_set_no"], "version": basis["rule"]["version"]}
                if basis.get("rule")
                else None
            )
            lot_check["reports_at_issue"] = [
                {"report_no": r["report_no"], "conclusion": r["conclusion"],
                 "lab_no": r["lab_no"]}
                for r in basis["reports"]
            ]
            lot_check["container_at_issue"] = basis.get("container")
            lot_check["issuer"] = decision["issuer"]
            lot_check["issued_at"] = decision["issued_at"]
            checks[lot_no] = lot_check
        return checks

    # 企业查阅 --------------------------------------------------------------
    def lot_bundle(self, lot_no: str, policy: AccessPolicy) -> dict[str, Any]:
        """企业只能查看自己的材料；跨部门协查按授权批次范围开放。"""
        lot = self._lot(lot_no)
        if not policy.can_read_lot(lot_no, lot["importer_no"], lot["producer_no"]):
            raise policy.deny_read()
        return {
            "lot": lot,
            "review": self.store.get("reviews", lot_no),
            "samples": [s for s in self.store.list("samples") if s["lot_no"] == lot_no],
            "reports": [r for r in self.store.list("reports") if r["lot_no"] == lot_no],
            "decisions": [
                {"decision_no": d["decision_no"], "kind": d["kind"], "scope": d["scope"],
                 "reason": d["reason"], "issued_at": d["issued_at"], "issuer": d["issuer"]}
                for d in self.store.list("decisions")
                if lot_no in d.get("lot_nos", [])
            ],
            "certificates": [
                c for c in self.store.list("certificates") if lot_no in c["lot_nos"]
            ],
        }
