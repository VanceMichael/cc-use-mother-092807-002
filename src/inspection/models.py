"""领域模型：状态枚举、键名约定与纯函数不变量。

记录统一使用 dict 表达，便于 JSON 持久化与断点恢复；
服务层负责构造与状态迁移，本模块只放枚举和不依赖存储的规则。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def is_overdue(due_at: str | None, moment: datetime) -> bool:
    if not due_at:
        return False
    return parse_iso(due_at) < moment


def stable_hash(payload: object) -> str:
    """对任意可 JSON 化内容计算稳定摘要，用于规则、回执、依据快照。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class EnterpriseStatus(str, Enum):
    REGISTERED = "registered"   # 在册有效
    SUSPENDED = "suspended"     # 暂停
    REVOKED = "revoked"         # 撤销


class LotStatus(str, Enum):
    DECLARED = "declared"               # 已申报
    DOC_REVIEWED = "doc_reviewed"       # 审单完成
    DETAINED = "detained"               # 扣留（含整票扣留）
    INSPECTING = "inspecting"           # 查验中
    PENDING_TEST = "pending_test"       # 待检测
    AWAIT_DISPOSAL = "await_disposal"   # 待处置
    SUSPENDED = "suspended"             # 暂停（回执异文等）
    SPLIT = "split"                     # 已按规格拆分（母批，不再单独处置）
    RELEASED = "released"               # 放行（终局）
    RETURNED = "returned"               # 退运（终局）
    DESTROYED = "destroyed"             # 销毁（终局）


TERMINAL_LOT_STATUSES = frozenset(
    {LotStatus.RELEASED, LotStatus.RETURNED, LotStatus.DESTROYED}
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class SampleStatus(str, Enum):
    COLLECTED = "collected"     # 已采集
    IN_TRANSIT = "in_transit"   # 运送中
    RECEIVED = "received"       # 实验室签收
    TESTING = "testing"         # 检测中
    RETAINED = "retained"       # 留样
    EXHAUSTED = "exhausted"     # 用尽
    DISPOSED = "disposed"       # 废弃


# 样品终态：恢复任务时不再追踪
TERMINAL_SAMPLE_STATUSES = frozenset(
    {SampleStatus.EXHAUSTED, SampleStatus.DISPOSED}
)


class SamplePurpose(str, Enum):
    PRIMARY = "primary"     # 初检样
    RETAIN = "retain"       # 留样
    RETEST = "retest"       # 复检样


class TestConclusion(str, Enum):
    QUALIFIED = "qualified"         # 合格
    UNQUALIFIED = "unqualified"     # 不合格
    INCONCLUSIVE = "inconclusive"   # 无法判定（需复检）


class DecisionKind(str, Enum):
    DETAIN = "detain"                       # 扣留
    NARROW_DETENTION = "narrow_detention"   # 部分解除扣留
    RELEASE = "release"                     # 放行
    RETURN = "return"                       # 退运
    DESTROY = "destroy"                     # 销毁


class DecisionScope(str, Enum):
    CONTAINER = "container"
    LOT = "lot"


class RectificationStatus(str, Enum):
    OPEN = "open"                       # 整改中（企业待提交）
    SUBMITTED = "submitted"             # 已提交待海关复核
    ACCEPTED = "accepted"               # 复核通过
    REJECTED = "rejected"               # 复核不通过，需重新整改


class CustodyAction(str, Enum):
    COLLECT = "collect"
    SPLIT = "split"
    HANDOVER = "handover"
    RECEIVE = "receive"
    DISPOSE = "dispose"
