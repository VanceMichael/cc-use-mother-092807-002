"""准入查验领域异常。

业务规则被破坏时抛出对应异常，便于现场处置系统区分
“需要人工核查”和“无权操作”等不同情形。
"""

from __future__ import annotations


class InspectionError(Exception):
    """领域规则异常基类。"""


class ValidationError(InspectionError):
    """资料不完整或数量、状态不满足约束。"""


class ConflictError(InspectionError):
    """整票扣留、局部放行或终局处置之间发生冲突。"""


class PermissionDenied(InspectionError):
    """主体无权查看材料或执行该环节。"""


class ImmutableRecordError(InspectionError):
    """放行、退运、销毁等终局事实不可覆盖。"""


class ReceiptConflict(InspectionError):
    """同编号渠道回执内容不一致，相关批次已暂停。"""

    def __init__(self, message_id: str, lot_no: str) -> None:
        super().__init__(f"渠道回执{message_id}内容与首次报送不一致，批次{lot_no}已暂停")
        self.message_id = message_id
        self.lot_no = lot_no
