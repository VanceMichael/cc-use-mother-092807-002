"""主体与权限。

按履职范围控制访问：
- 企业（进口商、境外生产企业）只能查看本企业材料；
- 取样人负责样品采集与拆分，不能独自确认最终合格；
- 海关监管人员、实验室人员、处置执行单位按职责授权；
- 跨部门协查凭授权的案件范围开放，最小授权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .errors import PermissionDenied


class Role(Enum):
    CUSTOMS = "customs"                 # 海关食品监管人员
    SAMPLER = "sampler"                 # 取样人
    LAB = "lab"                         # 实验室人员
    DISPOSAL = "disposal"               # 退运与销毁执行单位
    IMPORTER = "importer"               # 进口商
    OVERSEAS_PRODUCER = "producer"      # 境外生产企业
    CROSS_DEPT = "cross_dept"           # 跨部门协查人员


# 各角色可执行的动作
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.CUSTOMS: frozenset({
        "read_all",
        "rule.publish",
        "enterprise.register",
        "document.review",
        "inspection.create",
        "decision.issue",           # 签发扣留/放行/退运/销毁决定
        "rectification.review",
        "channel.receive",
    }),
    Role.SAMPLER: frozenset({
        "sample.collect",
        "sample.split",
        "sample.handover",
    }),
    Role.LAB: frozenset({
        "sample.receive",
        "test.report",
        "sample.archive",
    }),
    Role.DISPOSAL: frozenset({
        "decision.execute",         # 回填退运/销毁执行证明
    }),
    Role.IMPORTER: frozenset({"read_own"}),
    Role.OVERSEAS_PRODUCER: frozenset({"read_own"}),
    Role.CROSS_DEPT: frozenset(),
}

# 企业材料查看范围：进口商关联自己的批次；生产企业关联自己的产品
ENTERPRISE_ROLES = frozenset({Role.IMPORTER, Role.OVERSEAS_PRODUCER})


@dataclass(frozen=True)
class AccessPolicy:
    """主体及其履职范围。

    case_scopes 为该主体有权接触的批次编号集合：
    - 跨部门协查按授权案件的履职范围开放；
    - 实验室仅限承接了样品的批次。
    enterprise_no 为企业主体编号，只能查看本企业材料。
    """

    principal_id: str
    role: Role
    name: str = ""
    enterprise_no: str | None = None
    case_scopes: frozenset[str] = field(default_factory=frozenset)

    def can(self, action: str) -> bool:
        return action in PERMISSIONS.get(self.role, frozenset())

    def require(self, action: str) -> None:
        if not self.can(action):
            raise PermissionDenied(f"{self.role.value}无权执行{action}")

    def can_read_lot(self, lot_no: str, importer_no: str, producer_no: str) -> bool:
        if self.role == Role.CUSTOMS:
            return True
        if self.role in (Role.SAMPLER, Role.DISPOSAL):
            return True
        if self.role == Role.LAB:
            return lot_no in self.case_scopes
        if self.role in ENTERPRISE_ROLES:
            return self.enterprise_no in (importer_no, producer_no)
        if self.role == Role.CROSS_DEPT:
            return lot_no in self.case_scopes
        return False

    def deny_read(self) -> PermissionDenied:
        return PermissionDenied(f"{self.role.value}无权查看该材料")
