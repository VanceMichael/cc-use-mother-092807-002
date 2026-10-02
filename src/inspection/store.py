"""JSON 存储库：整库读取、原子落盘。

服务把全部记录放进一个 dict，按集合名分区；存储只负责持久化，
不理解业务规则，便于服务重启后完整恢复现场。
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any


COLLECTIONS = (
    "rule_sets",        # rule_set_no -> [版本记录...]
    "registrations",    # 企业编号 -> [注册版本...]
    "manifests",        # manifest_no -> 舱单（含箱货）
    "lots",             # lot_no -> 货批
    "reviews",          # lot_no -> 审单记录
    "tasks",            # task_no -> 查验任务
    "samples",          # sample_no -> 样品链节点
    "reports",          # report_no -> 检测报告
    "decisions",        # decision_no -> 决定（含依据快照，只增不改）
    "certificates",     # cert_no -> 退运/销毁证明
    "rectifications",   # rect_no -> 整改
    "receipts",         # message_id -> 首次回执摘要与结果
    "suspensions",      # lot_no -> 暂停记录
    "sequences",        # 编号计数器
)


class Store:
    """内存数据 + 可选 JSON 文件，落盘采用临时文件原子替换。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = pathlib.Path(path) if path else None
        if self.path and self.path.exists():
            self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            for name in COLLECTIONS:
                self.data.setdefault(name, {})
        else:
            self.data = {name: {} for name in COLLECTIONS}

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    # 通用访问 -------------------------------------------------
    def table(self, name: str) -> dict[str, Any]:
        return self.data[name]

    def put(self, collection: str, key: str, record: Any) -> None:
        self.data[collection][key] = record

    def get(self, collection: str, key: str) -> Any:
        return self.data[collection].get(key)

    def require(self, collection: str, key: str, label: str = "记录") -> Any:
        record = self.get(collection, key)
        if record is None:
            raise KeyError(f"{label}{key}不存在")
        return record

    def list(self, collection: str) -> list[Any]:
        return list(self.data[collection].values())

    def next_seq(self, prefix: str) -> int:
        counters = self.data["sequences"]
        value = int(counters.get(prefix, 0)) + 1
        counters[prefix] = value
        return value
