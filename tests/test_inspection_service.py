"""口岸食品准入查验与退运销毁闭环服务的行为测试。"""

import json
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.inspection_service import (
    ConflictError,
    DecisionType,
    InspectionService,
    ItemResult,
    PermissionDeniedError,
    PortionKind,
    Principal,
    QualificationError,
    ReceiptConflictError,
    RegistrationStatus,
    RiskRuleSet,
    Role,
    StateError,
    TestItem,
)

T0 = datetime(2026, 3, 1, 9, 0, 0)


def rules_v1() -> RiskRuleSet:
    return RiskRuleSet(
        version=1,
        effective_from=datetime(2026, 1, 1),
        required_docs=("卫生证书", "原产地证书"),
        high_risk_categories=("乳制品",),
        sample_sla_days=7,
        rectification_deadline_days=30,
        cold_chain_categories=("乳制品",),
        temperature_min=Decimal("-25"),
        temperature_max=Decimal("-18"),
    )


def rules_v2() -> RiskRuleSet:
    return RiskRuleSet(
        version=2,
        effective_from=T0,
        required_docs=("卫生证书", "原产地证书", "检测报告"),
        high_risk_categories=("乳制品",),
        sample_sla_days=5,
        rectification_deadline_days=15,
        cold_chain_categories=("乳制品",),
        temperature_min=Decimal("-25"),
        temperature_max=Decimal("-21"),
    )


class InspectionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = InspectionService()
        self.customs = Principal("CUS-1", Role.CUSTOMS, "CUSTOMS")
        self.sampler = Principal("CUS-2", Role.CUSTOMS, "CUSTOMS")
        self.confirmer = Principal("CUS-3", Role.CUSTOMS, "CUSTOMS")
        self.importer = Principal("IMP-1", Role.ENTERPRISE, "IMP-1")
        self.other_importer = Principal("IMP-2", Role.ENTERPRISE, "IMP-2")
        self.maker = Principal("ENT-1", Role.ENTERPRISE, "ENT-1")
        self.lab = Principal("LAB-1", Role.LAB, "LAB-1")
        self.other_lab = Principal("LAB-2", Role.LAB, "LAB-2")
        self.executor = Principal("EXE-1", Role.EXECUTOR, "EXE-1")
        self.collab = Principal("COL-1", Role.COLLABORATOR, "市场监管", ("处置协查",))
        service = self.service
        service.publish_rules(self.customs, rules_v1())
        service.register_manufacturer(
            self.customs,
            "REG-1",
            enterprise_id="ENT-1",
            enterprise_name="海外乳品有限公司",
            country="新西兰",
            categories=("乳制品",),
            valid_until=date(2027, 12, 31),
            now=T0,
        )
        service.qualify_product(
            self.customs,
            "QUA-1",
            "REG-1",
            category="乳制品",
            hs_code="040221",
            specs=("全脂奶粉", "脱脂奶粉"),
            now=T0,
        )
        service.declare_manifest(self.importer, "MAN-1", voyage="V2026-001", importer_id="IMP-1", now=T0)
        service.register_container(self.importer, "CON-1", "MAN-1", Decimal("1000"))
        service.record_temperature(self.customs, "TMP-1", "CON-1", celsius=Decimal("-20"), recorded_at=T0)
        service.declare_batch(
            self.importer,
            "BAT-1",
            "CON-1",
            "REG-1",
            "QUA-1",
            category="乳制品",
            hs_code="040221",
            lines={"全脂奶粉": Decimal("500"), "脱脂奶粉": Decimal("300")},
            now=T0,
        )
        service.complete_document_review(self.customs, "REV-BAT-1", True, T0)

    def _pass_spec(self, spec: str, sample_id: str, conclusion_id: str, batch_id: str = "BAT-1", now: datetime = T0) -> None:
        self.service.draw_sample(self.sampler, sample_id, batch_id, spec, Decimal("5"), "LAB-1", now)
        self.service.submit_conclusion(
            self.lab,
            conclusion_id,
            sample_id,
            (TestItem("菌落总数", ItemResult.PASS),),
            now,
        )
        self.service.confirm_conclusion(self.confirmer, conclusion_id, now)

    def _fail_spec(self, spec: str, sample_id: str, conclusion_id: str, now: datetime = T0) -> None:
        self.service.draw_sample(self.sampler, sample_id, "BAT-1", spec, Decimal("5"), "LAB-1", now)
        self.service.submit_conclusion(
            self.lab,
            conclusion_id,
            sample_id,
            (TestItem("黄曲霉毒素M1", ItemResult.FAIL, (spec,)),),
            now,
        )
        self.service.confirm_conclusion(self.confirmer, conclusion_id, now)

    # -- 资格准入 -------------------------------------------------------------

    def test_declaration_requires_valid_qualification(self) -> None:
        with self.assertRaisesRegex(QualificationError, "规格未获产品资格"):
            self.service.declare_batch(
                self.importer,
                "BAT-X",
                "CON-1",
                "REG-1",
                "QUA-1",
                category="乳制品",
                hs_code="040221",
                lines={"炼乳": Decimal("1")},
                now=T0,
            )
        self.service.change_registration(
            self.customs, "REG-1", T0, status=RegistrationStatus.SUSPENDED, reason="境外检查未通过"
        )
        with self.assertRaisesRegex(QualificationError, "注册无效"):
            self.service.declare_batch(
                self.importer,
                "BAT-Y",
                "CON-1",
                "REG-1",
                "QUA-1",
                category="乳制品",
                hs_code="040221",
                lines={"全脂奶粉": Decimal("1")},
                now=T0,
            )

    def test_container_split_batches_share_declared_quantity(self) -> None:
        self.service.declare_batch(
            self.importer,
            "BAT-2",
            "CON-1",
            "REG-1",
            "QUA-1",
            category="乳制品",
            hs_code="040221",
            lines={"全脂奶粉": Decimal("200")},
            now=T0,
        )
        with self.assertRaisesRegex(ConflictError, "申报货量"):
            self.service.declare_batch(
                self.importer,
                "BAT-3",
                "CON-1",
                "REG-1",
                "QUA-1",
                category="乳制品",
                hs_code="040221",
                lines={"脱脂奶粉": Decimal("1")},
                now=T0,
            )

    def test_release_requires_document_review(self) -> None:
        self.service.register_container(self.importer, "CON-8", "MAN-1", Decimal("100"))
        self.service.declare_batch(
            self.importer,
            "BAT-8",
            "CON-8",
            "REG-1",
            "QUA-1",
            category="乳制品",
            hs_code="040221",
            lines={"全脂奶粉": Decimal("100")},
            now=T0,
        )
        with self.assertRaisesRegex(StateError, "审单"):
            self.service.make_decision(
                self.customs,
                "DEC-8",
                "BAT-8",
                DecisionType.RELEASE,
                (("全脂奶粉", Decimal("100")),),
                T0,
            )

    # -- 样品链 ---------------------------------------------------------------

    def test_sample_split_and_retest_conserve_quantity(self) -> None:
        self.service.draw_sample(self.sampler, "SAM-1", "BAT-1", "全脂奶粉", Decimal("5"), "LAB-1", T0)
        self.service.split_sample(
            self.lab, "SAM-1", "SAM-1-RET", "SAM-1-P1", PortionKind.RETENTION, Decimal("2"), "留样库"
        )
        self.service.request_retest(self.customs, "SAM-1", "SAM-1-RT", Decimal("1"), "LAB-1", T0)
        self.assertTrue(self.service.sample_integrity("SAM-1"))
        sample = self.service.view_sample(self.customs, "SAM-1")
        locations = {portion.location for portion in sample.portions.values()}
        self.assertIn("留样库", locations)
        self.assertIn("实验室:LAB-1", locations)
        self.service.dispose_portion(self.lab, "SAM-1", "SAM-1-P1", "检测完毕已销毁")
        self.assertTrue(self.service.sample_integrity("SAM-1"))
        with self.assertRaisesRegex(StateError, "拆分数量"):
            self.service.split_sample(
                self.lab, "SAM-1", "SAM-1-X", "SAM-1-RET", PortionKind.RETENTION, Decimal("99"), "留样库"
            )
        with self.assertRaisesRegex(StateError, "留样不足"):
            self.service.request_retest(self.customs, "SAM-1", "SAM-1-RT2", Decimal("5"), "LAB-1", T0)

    def test_sampler_cannot_confirm_final_conclusion(self) -> None:
        self.service.draw_sample(self.sampler, "SAM-1", "BAT-1", "全脂奶粉", Decimal("5"), "LAB-1", T0)
        self.service.submit_conclusion(
            self.lab, "CONC-1", "SAM-1", (TestItem("菌落总数", ItemResult.PASS),), T0
        )
        with self.assertRaisesRegex(PermissionDeniedError, "取样人"):
            self.service.confirm_conclusion(self.sampler, "CONC-1", T0)
        conclusion = self.service.confirm_conclusion(self.confirmer, "CONC-1", T0)
        self.assertEqual(conclusion.confirmed_by, "CUS-3")

    def test_retest_can_override_initial_failure(self) -> None:
        self._fail_spec("全脂奶粉", "SAM-A", "CONC-A")
        self.service.split_sample(
            self.lab, "SAM-A", "SAM-A-RET", "SAM-A-P1", PortionKind.RETENTION, Decimal("2"), "留样库"
        )
        self.service.request_retest(self.customs, "SAM-A", "SAM-A-RT", Decimal("1"), "LAB-1", T0)
        later = T0 + timedelta(hours=2)
        self.service.submit_conclusion(
            self.lab, "CONC-A2", "SAM-A", (TestItem("黄曲霉毒素M1", ItemResult.PASS),), later
        )
        self.service.confirm_conclusion(self.confirmer, "CONC-A2", later)
        self.service.make_decision(
            self.customs, "DEC-1", "BAT-1", DecisionType.RELEASE, (("全脂奶粉", Decimal("500")),), later
        )
        view = self.service.view_batch(self.customs, "BAT-1")
        self.assertEqual(view["批次"]["lines"]["全脂奶粉"].released, Decimal("500"))

    # -- 扣留与放行互斥 ---------------------------------------------------------

    def test_partial_release_never_conflicts_with_detention(self) -> None:
        self._fail_spec("全脂奶粉", "SAM-A", "CONC-A")
        self._pass_spec("脱脂奶粉", "SAM-B", "CONC-B")
        # 一项不合格只影响全脂，脱脂可局部放行
        self.service.make_decision(
            self.customs, "DEC-1", "BAT-1", DecisionType.RELEASE, (("脱脂奶粉", Decimal("300")),), T0
        )
        # 不合格规格不得放行
        with self.assertRaisesRegex(ConflictError, "不合格"):
            self.service.make_decision(
                self.customs, "DEC-2", "BAT-1", DecisionType.RELEASE, (("全脂奶粉", Decimal("500")),), T0
            )
        # 整票扣留剩余全脂
        self.service.make_decision(
            self.customs, "DEC-3", "BAT-1", DecisionType.DETAIN, (("全脂奶粉", Decimal("500")),), T0
        )
        # 扣留中不得放行
        with self.assertRaisesRegex(ConflictError, "扣留"):
            self.service.make_decision(
                self.customs, "DEC-4", "BAT-1", DecisionType.RELEASE, (("全脂奶粉", Decimal("1")),), T0
            )
        # 已放行部分不得再扣留
        with self.assertRaisesRegex(ConflictError, "在控货量"):
            self.service.make_decision(
                self.customs, "DEC-5", "BAT-1", DecisionType.DETAIN, (("脱脂奶粉", Decimal("1")),), T0
            )
        # 解除扣留后货量回到在控
        self.service.make_decision(
            self.customs, "DEC-6", "BAT-1", DecisionType.LIFT_DETENTION, (("全脂奶粉", Decimal("500")),), T0
        )
        view = self.service.view_batch(self.customs, "BAT-1")
        self.assertEqual(view["批次"]["lines"]["全脂奶粉"].pending, Decimal("500"))
        self.assertEqual(view["批次"]["lines"]["脱脂奶粉"].released, Decimal("300"))

    # -- 规则换版 -----------------------------------------------------------------

    def test_rule_change_only_affects_future_decisions(self) -> None:
        self._pass_spec("脱脂奶粉", "SAM-B", "CONC-B")
        decision = self.service.make_decision(
            self.customs, "DEC-1", "BAT-1", DecisionType.RELEASE, (("脱脂奶粉", Decimal("300")),), T0
        )
        self.assertEqual(decision.basis.rule_version, 1)
        self.service.publish_rules(self.customs, rules_v2())
        # 已放行事实不被覆盖
        view = self.service.view_batch(self.customs, "BAT-1")
        self.assertEqual(view["批次"]["lines"]["脱脂奶粉"].released, Decimal("300"))
        # 新决定采用新版规则：温控上限收紧到-21，现有-20记录超限
        self.service.register_container(self.importer, "CON-2", "MAN-1", Decimal("1000"))
        self.service.record_temperature(self.customs, "TMP-2", "CON-2", celsius=Decimal("-20"), recorded_at=T0)
        self.service.declare_batch(
            self.importer,
            "BAT-2",
            "CON-2",
            "REG-1",
            "QUA-1",
            category="乳制品",
            hs_code="040221",
            lines={"全脂奶粉": Decimal("600")},
            now=T0,
        )
        self.service.complete_document_review(self.customs, "REV-BAT-2", True, T0)
        self._pass_spec("全脂奶粉", "SAM-C", "CONC-C", batch_id="BAT-2")
        with self.assertRaisesRegex(StateError, "温控"):
            self.service.make_decision(
                self.customs, "DEC-2", "BAT-2", DecisionType.RELEASE, (("全脂奶粉", Decimal("600")),), T0
            )
        # 旧决定仍按当时版本还原
        explanation = self.service.explain_decision(self.customs, "DEC-1")
        self.assertEqual(explanation["rules"].version, 1)
        self.assertEqual(explanation["rules"].temperature_max, Decimal("-18"))

    # -- 退运销毁 -----------------------------------------------------------------

    def test_return_and_destroy_need_approval_and_matching_certificate(self) -> None:
        self._fail_spec("全脂奶粉", "SAM-A", "CONC-A")
        # 未扣留不得处置
        with self.assertRaisesRegex(ConflictError, "扣留"):
            self.service.make_decision(
                self.customs, "DEC-R0", "BAT-1", DecisionType.RETURN, (("全脂奶粉", Decimal("1")),), T0
            )
        self.service.make_decision(
            self.customs, "DEC-D", "BAT-1", DecisionType.DETAIN, (("全脂奶粉", Decimal("500")),), T0
        )
        decision = self.service.make_decision(
            self.customs, "DEC-R", "BAT-1", DecisionType.RETURN, (("全脂奶粉", Decimal("500")),), T0
        )
        self.assertFalse(decision.executed)
        # 未批准不得出证
        with self.assertRaisesRegex(StateError, "批准"):
            self.service.record_certificate(
                self.executor, "CERT-1", "DEC-R", (("全脂奶粉", Decimal("500")),), "退运提单RT-001", T0
            )
        # 决定人不能自批
        with self.assertRaisesRegex(PermissionDeniedError, "批准人"):
            self.service.approve_decision(self.customs, "DEC-R", T0)
        self.service.approve_decision(self.confirmer, "DEC-R", T0)
        # 证明数量须与决定一致
        with self.assertRaisesRegex(ConflictError, "不一致"):
            self.service.record_certificate(
                self.executor, "CERT-1", "DEC-R", (("全脂奶粉", Decimal("400")),), "退运提单RT-001", T0
            )
        certificate = self.service.record_certificate(
            self.executor, "CERT-1", "DEC-R", (("全脂奶粉", Decimal("500")),), "退运提单RT-001", T0
        )
        # 重送同一证明返回原结果
        again = self.service.record_certificate(
            self.executor, "CERT-1", "DEC-R", (("全脂奶粉", Decimal("500")),), "退运提单RT-001", T0
        )
        self.assertIs(again, certificate)
        explanation = self.service.explain_decision(self.customs, "DEC-R")
        self.assertTrue(explanation["decision"].executed)
        self.assertEqual([a.approver_id for a in explanation["approvals"]], ["CUS-3"])
        # 已退运事实不可覆盖
        view = self.service.view_batch(self.customs, "BAT-1")
        self.assertEqual(view["批次"]["lines"]["全脂奶粉"].returned, Decimal("500"))

    # -- 渠道回执 -----------------------------------------------------------------

    def test_channel_receipt_replay_and_conflict(self) -> None:
        receipt = self.service.submit_channel_receipt(
            self.executor, "PORT", "RC-1", ("BAT-1",), {"decision": "DEC-R", "result": "已离境"}, T0
        )
        again = self.service.submit_channel_receipt(
            self.executor,
            "PORT",
            "RC-1",
            ("BAT-1",),
            {"decision": "DEC-R", "result": "已离境"},
            T0 + timedelta(hours=1),
        )
        self.assertIs(again, receipt)
        self.assertEqual(again.received_at, receipt.received_at)
        with self.assertRaisesRegex(ReceiptConflictError, "内容不同"):
            self.service.submit_channel_receipt(
                self.executor,
                "PORT",
                "RC-1",
                ("BAT-1",),
                {"decision": "DEC-R", "result": "未到港"},
                T0 + timedelta(hours=2),
            )
        # 相关批次被暂停，决定被阻断
        with self.assertRaisesRegex(StateError, "暂停"):
            self.service.make_decision(
                self.customs, "DEC-X", "BAT-1", DecisionType.DETAIN, (("全脂奶粉", Decimal("1")),), T0
            )
        self.service.resume_batch(self.customs, "BAT-1", T0)
        self.service.make_decision(
            self.customs, "DEC-X", "BAT-1", DecisionType.DETAIN, (("全脂奶粉", Decimal("1")),), T0
        )

    # -- 访问控制 -----------------------------------------------------------------

    def test_enterprise_sees_only_own_materials(self) -> None:
        view = self.service.view_batch(self.importer, "BAT-1")
        self.assertIn("决定", view)
        maker_view = self.service.view_batch(self.maker, "BAT-1")
        self.assertIn("资格", maker_view)
        with self.assertRaises(PermissionDeniedError):
            self.service.view_batch(self.other_importer, "BAT-1")

    def test_collaborator_access_follows_duties(self) -> None:
        view = self.service.view_batch(self.collab, "BAT-1")
        self.assertIn("决定", view)
        self.assertIn("证明", view)
        self.assertNotIn("审单", view)
        self.assertNotIn("温控", view)
        risk = Principal("COL-2", Role.COLLABORATOR, "卫健", ("风险预警",))
        risk_view = self.service.view_batch(risk, "BAT-1")
        self.assertIn("检测", risk_view)
        self.assertNotIn("证明", risk_view)
        no_duty = Principal("COL-3", Role.COLLABORATOR, "税务", ())
        with self.assertRaises(PermissionDeniedError):
            self.service.view_batch(no_duty, "BAT-1")

    def test_lab_sees_only_own_samples(self) -> None:
        self.service.draw_sample(self.sampler, "SAM-1", "BAT-1", "全脂奶粉", Decimal("5"), "LAB-1", T0)
        sample = self.service.view_sample(self.lab, "SAM-1")
        self.assertEqual(sample.sample_id, "SAM-1")
        with self.assertRaises(PermissionDeniedError):
            self.service.view_sample(self.other_lab, "SAM-1")
        view = self.service.view_batch(self.lab, "BAT-1")
        self.assertEqual(set(view), {"批次", "样品", "检测"})
        with self.assertRaises(PermissionDeniedError):
            self.service.view_batch(self.other_lab, "BAT-1")

    # -- 整改 -----------------------------------------------------------------

    def test_rectification_review_and_registration_suspension(self) -> None:
        self.service.open_rectification(self.customs, "RECT-1", "REG-1", "检出生殖毒素超标", T0)
        with self.assertRaises(PermissionDeniedError):
            self.service.submit_rectification(self.other_importer, "RECT-1", "已更换奶源", T0)
        self.service.submit_rectification(self.maker, "RECT-1", "已更换奶源并复检", T0)
        self.service.review_rectification(self.customs, "RECT-1", False, T0)
        registration = self.service.registration_at("REG-1")
        self.assertIs(registration.status, RegistrationStatus.SUSPENDED)
        self.assertEqual(registration.version, 2)

    # -- 恢复与溯源 -------------------------------------------------------------

    def test_recovery_resumes_pending_work_after_restart(self) -> None:
        later = T0 + timedelta(days=8)
        # 超期样品：在检，期限 T0+7
        self.service.draw_sample(self.sampler, "SAM-1", "BAT-1", "全脂奶粉", Decimal("5"), "LAB-1", T0)
        # 待处置货物：扣留全脂100
        self.service.make_decision(
            self.customs, "DEC-D", "BAT-1", DecisionType.DETAIN, (("全脂奶粉", Decimal("100")),), T0
        )
        # 整改复核任务
        self.service.open_rectification(self.customs, "RECT-1", "REG-1", "标签不合格", T0)
        self.service.submit_rectification(self.maker, "RECT-1", "已更换标签", T0)
        snapshot = self.service.to_snapshot()
        restored = InspectionService.from_snapshot(json.loads(json.dumps(snapshot, ensure_ascii=False)))
        self.assertEqual(restored.to_snapshot(), self.service.to_snapshot())
        plan = restored.recover(later)
        self.assertEqual(plan.overdue_samples, ("SAM-1",))
        self.assertEqual(plan.pending_disposal_batches, ("BAT-1",))
        self.assertEqual(plan.pending_rectification_reviews, ("RECT-1",))
        # 恢复后继续办理：超期样品出结论、扣留货物退运、整改复核
        restored.submit_conclusion(
            self.lab, "CONC-1", "SAM-1", (TestItem("菌落总数", ItemResult.PASS),), later
        )
        restored.make_decision(
            self.customs, "DEC-R", "BAT-1", DecisionType.RETURN, (("全脂奶粉", Decimal("100")),), later
        )
        restored.approve_decision(self.confirmer, "DEC-R", later)
        restored.record_certificate(
            self.executor, "CERT-1", "DEC-R", (("全脂奶粉", Decimal("100")),), "退运提单RT-100", later
        )
        restored.review_rectification(self.customs, "RECT-1", True, later)
        self.assertEqual(restored.recover(later).pending_disposal_batches, ())

    def test_decision_basis_can_be_reconstructed(self) -> None:
        self._pass_spec("全脂奶粉", "SAM-1", "CONC-1")
        self.service.make_decision(
            self.customs, "DEC-1", "BAT-1", DecisionType.RELEASE, (("全脂奶粉", Decimal("500")),), T0
        )
        # 资格随后变更，历史版本仍保留
        self.service.change_registration(
            self.customs, "REG-1", T0, categories=("乳制品", "保健食品"), reason="扩项"
        )
        explanation = self.service.explain_decision(self.customs, "DEC-1")
        self.assertEqual(explanation["registration"].version, 1)
        self.assertEqual(explanation["registration"].categories, ("乳制品",))
        self.assertEqual(explanation["qualification"].specs, ("全脂奶粉", "脱脂奶粉"))
        self.assertEqual(explanation["rules"].version, 1)
        self.assertEqual([c.conclusion_id for c in explanation["conclusions"]], ["CONC-1"])
        self.assertEqual([r.record_id for r in explanation["temperature_records"]], ["TMP-1"])
        self.assertEqual(explanation["decision"].decided_by, "CUS-1")
        with self.assertRaises(PermissionDeniedError):
            self.service.explain_decision(self.other_importer, "DEC-1")

    def test_cold_chain_release_requires_temperature_records(self) -> None:
        self.service.register_container(self.importer, "CON-9", "MAN-1", Decimal("100"))
        self.service.declare_batch(
            self.importer,
            "BAT-9",
            "CON-9",
            "REG-1",
            "QUA-1",
            category="乳制品",
            hs_code="040221",
            lines={"全脂奶粉": Decimal("100")},
            now=T0,
        )
        self.service.complete_document_review(self.customs, "REV-BAT-9", True, T0)
        self._pass_spec("全脂奶粉", "SAM-9", "CONC-9", batch_id="BAT-9")
        with self.assertRaisesRegex(StateError, "温控"):
            self.service.make_decision(
                self.customs, "DEC-9", "BAT-9", DecisionType.RELEASE, (("全脂奶粉", Decimal("100")),), T0
            )
        self.service.record_temperature(self.customs, "TMP-9", "CON-9", celsius=Decimal("-10"), recorded_at=T0)
        with self.assertRaisesRegex(StateError, "温控"):
            self.service.make_decision(
                self.customs, "DEC-9", "BAT-9", DecisionType.RELEASE, (("全脂奶粉", Decimal("100")),), T0
            )


if __name__ == "__main__":
    unittest.main()
