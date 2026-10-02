"""准入查验领域服务测试。

覆盖：资格与规则换版、箱货拆批与整票/局部扣留冲突、货量与样品守恒、
复检、回执幂等与异文暂停、角色权限、终局不可覆盖、断点恢复、依据还原。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.inspection import (
    AccessPolicy,
    ConflictError,
    DecisionKind,
    DecisionScope,
    EnterpriseStatus,
    ImmutableRecordError,
    InspectionService,
    LotStatus,
    PermissionDenied,
    ReceiptConflict,
    Role,
    SamplePurpose,
    Store,
    TestConclusion,
    ValidationError,
)

RULES_V1 = [{"rule_no": "R1", "name": "单证一致性"}, {"rule_no": "R2", "name": "标签合规"}]
RULES_V2 = [{"rule_no": "R1", "name": "单证一致性"}, {"rule_no": "R2", "name": "标签合规（修订）"}]


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 1, 9, 0, 0)

    def __call__(self) -> str:
        return self.t.isoformat(timespec="seconds")

    def advance(self, **kwargs: int) -> str:
        self.t += timedelta(**kwargs)
        return self()

    def ago(self, **kwargs: int) -> str:
        return (self.t - timedelta(**kwargs)).isoformat(timespec="seconds")

    def later(self, **kwargs: int) -> str:
        return (self.t + timedelta(**kwargs)).isoformat(timespec="seconds")


class InspectionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.service = InspectionService(Store(), clock=self.clock)
        self.customs = AccessPolicy("U-CUST-01", Role.CUSTOMS, "海关监管员")
        self.sampler = AccessPolicy("U-SAMP-01", Role.SAMPLER, "取样人")
        self.lab = AccessPolicy("LAB-01", Role.LAB, "检测实验室")
        self.disposal = AccessPolicy("DISP-01", Role.DISPOSAL, "处置单位")
        self.importer = AccessPolicy("CO-IMP-01", Role.IMPORTER, "进口商甲", enterprise_no="IMP-1")
        self.other_importer = AccessPolicy(
            "CO-IMP-02", Role.IMPORTER, "进口商乙", enterprise_no="IMP-2"
        )
        self.producer = AccessPolicy(
            "CO-PRO-01", Role.OVERSEAS_PRODUCER, "境外企业", enterprise_no="PROD-1"
        )

    def bootstrap(self, rules=RULES_V1) -> None:
        self.service.publish_rule_set("RS-1", "进口食品审单规则", rules, self.customs)
        self.service.register_enterprise(
            "PROD-1", "境外某食品厂", ["FOOD-A", "FOOD-B"], self.customs
        )
        self.service.submit_manifest(
            "M-1",
            [
                {
                    "container_no": "C1",
                    "seal_no": "SEAL-1",
                    "temperature_log": [{"at": self.clock(), "temp_c": 4.0}],
                }
            ],
            self.customs,
        )

    def declare(self, lot_no: str = "LOT-A", qty: float = 100.0, spec: str = "500g/罐"):
        return self.service.declare_lot(
            lot_no, "M-1", "C1", "IMP-1", "PROD-1", "FOOD-A", spec, qty, "kg",
            ["DOC-INV-1", "DOC-CO-1"], self.customs,
        )

    def qualified_flow(self, lot_no: str = "LOT-A", retain: float = 1.0):
        """审单→查验→抽样→检测合格（不自动放行），返回样品编号。"""
        self.service.review_documents(lot_no, self.customs)
        self.service.create_task(lot_no, "现场查验", self.sampler.principal_id,
                                 self.clock.later(days=1), self.customs)
        samples = self.service.collect_samples(
            lot_no,
            [{"qty": 2.0, "purpose": SamplePurpose.PRIMARY.value},
             {"qty": retain, "purpose": SamplePurpose.RETAIN.value}],
            self.sampler, lab_no="LAB-01", due_at=self.clock.later(days=7),
        )
        primary, retained = samples
        self.service.handover_sample(primary["sample_no"], self.sampler)
        self.service.receive_sample(primary["sample_no"], self.lab)
        self.service.report_test(
            primary["sample_no"], TestConclusion.QUALIFIED,
            [{"item": "微生物", "result": "合格"}], self.lab,
        )
        return primary["sample_no"], retained["sample_no"]


class TestQualifiedReleaseAndBasis(InspectionCase):
    def test_full_flow_balance_release_and_reconstruct(self) -> None:
        self.bootstrap()
        lot = self.declare()
        _, retain_no = self.qualified_flow()

        lot = self.service.store.get("lots", "LOT-A")
        # 97 在库 + 1 留样在链 + 2 已耗检测 = 100
        self.assertEqual(lot["remaining_qty"], 97.0)
        self.assertEqual(lot["consumed_sample_qty"], 2.0)
        # 检测合格不自动放行，最终合格必须海关批准
        self.assertEqual(lot["status"], LotStatus.PENDING_TEST.value)

        decision = self.service.release_lots(["LOT-A"], "检测合格准予入境", self.customs)
        self.assertEqual(self.service._lot("LOT-A")["status"], LotStatus.RELEASED.value)

        restored = self.service.reconstruct_decision(decision["decision_no"], self.customs)
        basis = restored["basis"]["LOT-A"]
        self.assertTrue(restored["immutable"])
        self.assertTrue(
            restored["basis_verification"]["LOT-A"]["basis_hash_intact"]
        )
        self.assertEqual(basis["registration"]["version"], 1)
        self.assertEqual(basis["rule"]["version"], 1)
        self.assertEqual(basis["reports"][0]["conclusion"], TestConclusion.QUALIFIED.value)
        self.assertEqual(basis["container"]["container_no"], "C1")
        self.assertEqual(restored["issuer"], self.customs.principal_id)
        # 留样仍可在依据中追溯去向
        self.assertIn(retain_no, {s["sample_no"] for s in basis["samples"]})

    def test_sampler_cannot_confirm_final_qualified(self) -> None:
        self.bootstrap()
        self.declare()
        self.qualified_flow()
        with self.assertRaises(PermissionDenied):
            self.service.release_lots(["LOT-A"], "取样人自行放行", self.sampler)


class TestRuleVersioning(InspectionCase):
    def test_rule_revision_does_not_rewrite_prior_decision(self) -> None:
        self.bootstrap()
        self.declare()
        self.qualified_flow()
        old = self.service.release_lots(["LOT-A"], "按V1放行", self.customs)

        # 换版只影响之后形成的决定
        self.clock.advance(days=30)
        self.service.publish_rule_set("RS-1", "进口食品审单规则", RULES_V2, self.customs)
        self.declare("LOT-B")
        review_b = self.service.review_documents("LOT-B", self.customs)
        self.assertEqual(review_b["rule_version"], 2)

        old_basis = self.service.reconstruct_decision(old["decision_no"], self.customs)
        self.assertEqual(old_basis["basis"]["LOT-A"]["rule"]["version"], 1)
        self.assertNotEqual(
            old_basis["basis"]["LOT-A"]["rule"]["content_hash"],
            self.service.rule_set_effective("RS-1")["content_hash"],
        )

    def test_not_yet_effective_rules_cannot_be_used(self) -> None:
        self.bootstrap()
        future = self.clock.later(days=10)
        self.service.publish_rule_set(
            "RS-2", "未生效规则", RULES_V2, self.customs, effective_at=future
        )
        with self.assertRaises(ValidationError):
            self.service.rule_set_effective("RS-2", self.clock())


class TestSplitAndDetentionConflict(InspectionCase):
    def test_split_quantity_conservation(self) -> None:
        self.bootstrap()
        self.declare(qty=100.0)
        with self.assertRaises(ValidationError):
            self.service.split_lot(
                "LOT-A",
                [{"spec": "500g/罐", "quantity": 60.0}, {"spec": "1kg/罐", "quantity": 30.0}],
                self.customs,
            )
        children = self.service.split_lot(
            "LOT-A",
            [{"spec": "500g/罐", "quantity": 60.0}, {"spec": "1kg/罐", "quantity": 40.0}],
            self.customs,
        )
        self.assertEqual(len(children), 2)
        self.assertEqual(self.service._lot("LOT-A")["status"], LotStatus.SPLIT.value)
        self.assertTrue(all(c["root_lot_no"] == "LOT-A" for c in children))
        self.assertEqual(sum(c["declared_qty"] for c in children), 100.0)

    def test_container_detain_blocks_partial_release_until_narrowed(self) -> None:
        self.bootstrap()
        self.declare("LOT-A1", qty=60.0)
        self.declare("LOT-A2", qty=40.0)

        detain = self.service.detain_container("M-1", "C1", "现场封识异常整票扣留", self.customs)
        self.assertEqual(detain["scope"], DecisionScope.CONTAINER.value)
        self.assertEqual(
            {self.service._lot(n)["status"] for n in ("LOT-A1", "LOT-A2")},
            {LotStatus.DETAINED.value},
        )

        # 未缩小扣留范围前，任一货批都不能放行（扣留冲突先于审单被拦截）
        with self.assertRaises(ConflictError):
            self.service.release_lots(["LOT-A2"], "试图与整票扣留冲突放行", self.customs)

        # 一项不合格只影响规格 A1：缩小扣留范围
        narrowed = self.service.narrow_detention(
            detain["decision_no"], ["LOT-A1"], "仅500g规格标签不合格", self.customs
        )
        self.assertEqual(narrowed["supersedes"], detain["decision_no"])
        self.assertEqual(self.service._lot("LOT-A1")["status"], LotStatus.DETAINED.value)
        self.assertEqual(self.service._lot("LOT-A2")["status"], LotStatus.DECLARED.value)

        # A2 走合格流程后放行；A1 检测不合格后销毁
        self.qualified_flow("LOT-A2")
        release = self.service.release_lots(["LOT-A2"], "未涉不合格规格", self.customs)
        self.assertEqual(self.service._lot("LOT-A2")["status"], LotStatus.RELEASED.value)

        samples = self.service.collect_samples(
            "LOT-A1", [{"qty": 2.0, "purpose": SamplePurpose.PRIMARY.value}], self.sampler
        )
        self.service.report_test(
            samples[0]["sample_no"], TestConclusion.UNQUALIFIED,
            [{"item": "标签", "result": "不合格"}], self.lab,
        )
        self.assertEqual(self.service._lot("LOT-A1")["status"], LotStatus.AWAIT_DISPOSAL.value)
        destroy = self.service.dispose_lots(
            DecisionKind.DESTROY, ["LOT-A1"], "标签不合格销毁", self.customs
        )
        cert = self.service.execute_disposal(
            destroy["decision_no"], "PROOF-DESTROY-1", "高温焚烧", self.disposal
        )
        self.assertEqual(self.service._lot("LOT-A1")["status"], LotStatus.DESTROYED.value)
        with self.assertRaises(ImmutableRecordError):
            self.service.execute_disposal(
                destroy["decision_no"], "PROOF-2", "再次销毁", self.disposal
            )
        # 销毁决定同样可完整还原依据
        restored = self.service.reconstruct_decision(destroy["decision_no"], self.customs)
        self.assertEqual(
            restored["basis_verification"]["LOT-A1"]["reports_at_issue"][0]["conclusion"],
            TestConclusion.UNQUALIFIED.value,
        )
        self.assertTrue(cert["cert_no"].startswith("CERT-"))


class TestSampleChain(InspectionCase):
    def test_split_and_retest_keep_quantity_consistent(self) -> None:
        self.bootstrap()
        self.declare()
        self.service.review_documents("LOT-A", self.customs)
        samples = self.service.collect_samples(
            "LOT-A",
            [{"qty": 2.0, "purpose": SamplePurpose.PRIMARY.value},
             {"qty": 1.0, "purpose": SamplePurpose.RETAIN.value}],
            self.sampler, due_at=self.clock.later(days=7),
        )
        primary, retain = samples
        # 初检无法判定
        self.service.report_test(
            primary["sample_no"], TestConclusion.INCONCLUSIVE,
            [{"item": "农残", "result": "可疑"}], self.lab, consume=True,
        )
        self.assertEqual(self.service._lot("LOT-A")["status"], LotStatus.PENDING_TEST.value)

        # 复检必须从留样分出
        retest = self.service.request_retest("LOT-A", 0.4, self.customs)
        self.assertEqual(retest["purpose"], SamplePurpose.RETEST.value)
        self.assertEqual(retest["parent_sample_no"], retain["sample_no"])
        self.assertEqual(self.service.store.get("samples", retain["sample_no"])["qty"], 0.6)
        # 留样 0.6 + 复检样 0.4 = 原留样 1.0；总盘库守恒
        self.service.check_lot_balance("LOT-A")

        self.service.receive_sample(retest["sample_no"], self.lab)
        self.service.report_test(
            retest["sample_no"], TestConclusion.QUALIFIED,
            [{"item": "农残", "result": "合格"}], self.lab, consume=True,
        )
        self.service.release_lots(["LOT-A"], "复检合格放行", self.customs)

    def test_retest_without_retain_rejected(self) -> None:
        self.bootstrap()
        self.declare()
        self.service.review_documents("LOT-A", self.customs)
        samples = self.service.collect_samples(
            "LOT-A", [{"qty": 2.0, "purpose": SamplePurpose.PRIMARY.value}], self.sampler
        )
        self.service.report_test(
            samples[0]["sample_no"], TestConclusion.INCONCLUSIVE, [], self.lab
        )
        with self.assertRaises(ValidationError):
            self.service.request_retest("LOT-A", 0.5, self.customs)

    def test_oversampling_rejected(self) -> None:
        self.bootstrap()
        self.declare(qty=10.0)
        self.service.review_documents("LOT-A", self.customs)
        with self.assertRaises(ValidationError):
            self.service.collect_samples(
                "LOT-A", [{"qty": 11.0, "purpose": SamplePurpose.PRIMARY.value}], self.sampler
            )

    def test_balance_tampering_detected(self) -> None:
        self.bootstrap()
        self.declare()
        self.qualified_flow()
        lot = self.service._lot("LOT-A")
        lot["remaining_qty"] = 90.0
        with self.assertRaises(ValidationError):
            self.service.check_lot_balance("LOT-A")


class TestReceipts(InspectionCase):
    def _two_lots(self):
        self.bootstrap()
        self.declare("LOT-A")
        self.qualified_flow("LOT-A")
        self.service.release_lots(["LOT-A"], "已放行", self.customs)
        self.declare("LOT-B", qty=10.0)

    def test_resend_same_receipt_returns_original_result(self) -> None:
        self._two_lots()
        payload = {"channel": "单一窗口", "status": "accepted"}
        first = self.service.receive_channel_receipt("MSG-1", "LOT-B", payload, self.customs)
        second = self.service.receive_channel_receipt("MSG-1", "LOT-B", dict(payload), self.customs)
        self.assertFalse(first["dedup"])
        self.assertTrue(second["dedup"])
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(len(self.service.store.list("receipts")), 1)

    def test_same_id_different_content_suspends_related_lots(self) -> None:
        self._two_lots()
        self.service.receive_channel_receipt(
            "MSG-2", "LOT-B", {"status": "accepted"}, self.customs
        )
        with self.assertRaises(ReceiptConflict):
            self.service.receive_channel_receipt(
                "MSG-2", "LOT-B", {"status": "revoked"}, self.customs
            )
        # 相关批次暂停；已经放行的终态事实不能被覆盖
        self.assertEqual(self.service._lot("LOT-B")["status"], LotStatus.SUSPENDED.value)
        self.assertEqual(self.service._lot("LOT-A")["status"], LotStatus.RELEASED.value)
        with self.assertRaises(ConflictError):
            self.service.release_lots(["LOT-B"], "暂停期间放行", self.customs)

        # 人工核查后恢复到暂停前状态
        self.service.resolve_suspension("MSG-2", "LOT-B", self.customs)
        self.assertEqual(self.service._lot("LOT-B")["status"], LotStatus.DECLARED.value)


class TestAccessControl(InspectionCase):
    def test_enterprise_sees_only_own_material(self) -> None:
        self.bootstrap()
        self.declare()
        self.qualified_flow()
        decision = self.service.release_lots(["LOT-A"], "放行", self.customs)
        bundle = self.service.lot_bundle("LOT-A", self.importer)
        self.assertEqual(bundle["lot"]["lot_no"], "LOT-A")
        # 当事企业可还原本批次放行依据，他企业不可见
        restored = self.service.reconstruct_decision(decision["decision_no"], self.importer)
        self.assertEqual(restored["decision_no"], decision["decision_no"])
        self.assertTrue(
            self.service.lot_bundle(
                "LOT-A",
                AccessPolicy("CO-PRO-01", Role.OVERSEAS_PRODUCER, enterprise_no="PROD-1"),
            )
        )
        with self.assertRaises(PermissionDenied):
            self.service.lot_bundle("LOT-A", self.other_importer)
        with self.assertRaises(PermissionDenied):
            self.service.reconstruct_decision(decision["decision_no"], self.other_importer)

    def test_cross_department_scoped_by_case(self) -> None:
        self.bootstrap()
        self.declare("LOT-A")
        self.declare("LOT-B")
        scoped = AccessPolicy("CD-1", Role.CROSS_DEPT, case_scopes=frozenset({"LOT-A"}))
        self.service.lot_bundle("LOT-A", scoped)
        with self.assertRaises(PermissionDenied):
            self.service.lot_bundle("LOT-B", scoped)

    def test_lab_scoped_to_assigned_lots(self) -> None:
        self.bootstrap()
        self.declare()
        unassigned = AccessPolicy("LAB-99", Role.LAB)
        with self.assertRaises(PermissionDenied):
            self.service.lot_bundle("LOT-A", unassigned)
        assigned = AccessPolicy("LAB-01", Role.LAB, case_scopes=frozenset({"LOT-A"}))
        self.service.lot_bundle("LOT-A", assigned)

    def test_enterprise_cannot_register_or_issue(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.register_enterprise(
                "PROD-9", "自封注册", ["X"], self.importer
            )


class TestImmutabilityAndEligibility(InspectionCase):
    def test_terminal_decisions_cannot_be_overwritten(self) -> None:
        self.bootstrap()
        self.declare()
        self.qualified_flow()
        self.service.release_lots(["LOT-A"], "放行", self.customs)
        with self.assertRaises(ImmutableRecordError):
            self.service.release_lots(["LOT-A"], "重复放行", self.customs)
        with self.assertRaises(ImmutableRecordError):
            self.service.review_documents("LOT-A", self.customs)

    def test_revoked_registration_and_product_scope_block_release(self) -> None:
        self.bootstrap()
        self.declare("LOT-A")
        self.qualified_flow("LOT-A")
        # 资格在放行决定时点核验：企业被撤销
        self.clock.advance(days=1)
        self.service.register_enterprise(
            "PROD-1", "境外某食品厂", ["FOOD-A"], self.customs,
            status=EnterpriseStatus.REVOKED,
        )
        with self.assertRaises(ConflictError):
            self.service.release_lots(["LOT-A"], "企业已撤销不应放行", self.customs)

        # 恢复企业但缩小产品范围
        self.service.register_enterprise(
            "PROD-1", "境外某食品厂", ["FOOD-C"], self.customs,
            status=EnterpriseStatus.REGISTERED,
        )
        with self.assertRaises(ConflictError):
            self.service.release_lots(["LOT-A"], "产品不在获准范围", self.customs)


class TestRectification(InspectionCase):
    def test_submit_and_review_rectification(self) -> None:
        self.bootstrap()
        self.declare()
        rect = self.service.open_rectification(
            "LOT-A", "整改标签中文标识", self.clock.later(days=7), self.customs
        )
        self.service.submit_rectification(rect["rect_no"], "已加贴中文标签", self.producer)
        reviewed = self.service.review_rectification(
            rect["rect_no"], True, "复核通过", self.customs
        )
        self.assertEqual(reviewed["status"], "accepted")
        with self.assertRaises(PermissionDenied):
            self.service.submit_rectification(rect["rect_no"], "无关企业提交", self.other_importer)

    def test_only_party_enterprise_may_submit(self) -> None:
        self.bootstrap()
        self.declare()
        rect = self.service.open_rectification("LOT-A", "整改", self.clock.later(days=3), self.customs)
        with self.assertRaises(PermissionDenied):
            self.service.submit_rectification(rect["rect_no"], "材料", self.other_importer)


class TestRecovery(unittest.TestCase):
    def test_recover_overdue_samples_lots_and_rectifications(self) -> None:
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            service = InspectionService(Store(path), clock=clock)
            customs = AccessPolicy("U-CUST-01", Role.CUSTOMS)
            sampler = AccessPolicy("U-SAMP-01", Role.SAMPLER)
            lab = AccessPolicy("LAB-01", Role.LAB)
            producer = AccessPolicy("CO-PRO-01", Role.OVERSEAS_PRODUCER, enterprise_no="PROD-1")

            service.publish_rule_set("RS-1", "规则", RULES_V1, customs)
            service.register_enterprise("PROD-1", "厂", ["FOOD-A"], customs)
            service.submit_manifest("M-1", [{"container_no": "C1"}], customs)

            # 待处置货物
            service.declare_lot(
                "LOT-1", "M-1", "C1", "IMP-1", "PROD-1", "FOOD-A", "500g", 10, "kg",
                ["D1"], customs,
            )
            service.review_documents("LOT-1", customs)
            s = service.collect_samples(
                "LOT-1", [{"qty": 2.0, "purpose": SamplePurpose.PRIMARY.value}], sampler,
                due_at=clock.ago(hours=1),
            )
            service.report_test(s[0]["sample_no"], TestConclusion.UNQUALIFIED, [], lab)

            # 超期查验任务，以及未送出的超期限留样
            service.declare_lot(
                "LOT-2", "M-1", "C1", "IMP-1", "PROD-1", "FOOD-A", "1kg", 5, "kg",
                ["D2"], customs,
            )
            service.review_documents("LOT-2", customs)
            service.collect_samples(
                "LOT-2",
                [{"qty": 1.0, "purpose": SamplePurpose.RETAIN.value}],
                sampler, due_at=clock.ago(hours=2),
            )
            service.create_task("LOT-2", "查验", "U-SAMP-01", clock.ago(hours=2), customs)

            # 超期整改（企业待提交）
            rect = service.open_rectification("LOT-2", "整改", clock.ago(hours=3), customs)

            service.store.save()

            # 模拟服务重启
            restarted = InspectionService(Store(path), clock=clock)
            self.assertEqual(len(restarted.store.list("lots")), 2)
            summary = restarted.recover(at=clock())
            overdue_sample_lots = {item["lot_no"] for item in summary["overdue_samples"]}
            # 用尽样品不再追踪；超期的是 LOT-2 尚未送出的留样
            self.assertEqual(overdue_sample_lots, {"LOT-2"})
            awaiting = {item["lot_no"] for item in summary["await_disposal_lots"]}
            self.assertIn("LOT-1", awaiting)
            overdue_rects = {item["rect_no"] for item in summary["overdue_rectifications"]}
            self.assertIn(rect["rect_no"], overdue_rects)
            open_task_lots = {t["lot_no"] for t in summary["open_tasks"]}
            self.assertIn("LOT-2", open_task_lots)


if __name__ == "__main__":
    unittest.main()
