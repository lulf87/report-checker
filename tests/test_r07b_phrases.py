from __future__ import annotations

import unittest

from mvp.full_report_attempt import evaluate_numeric_measurement


class AcceptancePhraseTests(unittest.TestCase):
    """Acceptance wordings first seen in sample 3944 (items 138 and 140)."""

    def check(self, requirement: str, result: str, status: str, value: str) -> None:
        decision = evaluate_numeric_measurement(requirement, result)
        self.assertEqual(decision["status"], status, decision)
        self.assertEqual(str(decision["criterion"]["value"]), value)

    def test_not_more_than_phrase(self) -> None:
        requirement = "一个未启动的单极患者电路分别经200Ω 负载到地和到中性电极均不应产生150mA 以上 高频电流。 单位：mA"
        self.check(requirement, "11", "pass", "150")
        self.check(requirement, "151", "error", "150")
        self.check("一个未启动的双极患者电路在跨接于两输 出端上的200Ω负载上不得产生50mA 以上高 频电流； 单位：mA", "2", "pass", "50")

    def test_changed_limit_phrase(self) -> None:
        requirement = "当直接在高频手术设备端口测量高频漏电流 时，前面的试验项目a) 也可通过将1) 和2) 的限值改变为100mA， 单位：mA"
        self.check(requirement, "66", "pass", "100")
        self.check(requirement, "120", "error", "100")

    def test_limit_computed_in_requirement(self) -> None:
        monopolar = (
            "应限制绝缘外表面上流 通的高频漏电流小于I 漏； 预期用于单极应用的手术附件的限值为： "
            "I [mA]=2.0×10−5×d×L×f × 漏 试 U =135.7[mA] P 单位：mA"
        )
        self.check(monopolar, "24.5", "pass", "135.7")
        bipolar = "预期用于双极应用的手术附件的相应限值为： I [mA]=4.0×10−5×d×L×f × 漏 试 U =201.6[mA] P 单位：mA"
        self.check(bipolar, "17.8", "pass", "201.6")
        self.check(bipolar, "250", "error", "201.6")

    def test_alternative_tolerance_row(self) -> None:
        # Sample 1998 item 197: "负载阻抗显示误差不超过±5%" + its own row "或±5Ω（取较大值）".
        passed = evaluate_numeric_measurement("或±5Ω（取较大值）。 单位：Ω", "+2")
        self.assertEqual(passed["status"], "pass")
        self.assertEqual((str(passed["criterion"]["low"]), str(passed["criterion"]["high"])), ("-5", "5"))
        self.assertEqual(evaluate_numeric_measurement("或±5Ω（取较大值）。 单位：Ω", "+7")["status"], "error")

    def test_condition_clause_is_not_the_limit(self) -> None:
        decision = evaluate_numeric_measurement("在压力值≤20g 时，误差不超过±3g; 单位：g", "-2.4～+3.0")
        self.assertEqual(decision["status"], "pass")
        self.assertIn("3", decision["criterion"]["source"])
        self.assertNotIn("20", decision["criterion"]["source"])
        self.assertEqual(evaluate_numeric_measurement("在压力值≤20g 时，误差不超过±3g; 单位：g", "-2.4～+4.0")["status"], "error")

    def test_nominal_equal_to_tolerance_decides_when_both_readings_agree(self) -> None:
        # Sample 0539 item 50: "0.2mV：±0.2mV", result "+0.0" passes as a deviation and as a value.
        self.assertEqual(evaluate_numeric_measurement("0.2mV ：± 0.2 mV | 正向", "+0.0")["status"], "pass")
        # "+0.3": deviation fails, value 0.3 within 0～0.4 passes -> a person decides.
        self.assertEqual(evaluate_numeric_measurement("0.2mV ：± 0.2 mV | 正向", "+0.3")["status"], "manual")

    def test_comma_separated_asymmetric_percent(self) -> None:
        self.assertEqual(evaluate_numeric_measurement("当 40J 时，允差：-10%，+5%。", "-9%")["status"], "pass")
        self.assertEqual(evaluate_numeric_measurement("当 40J 时，允差：-10%，+5%。", "+6%")["status"], "error")

    def test_value_range_with_compound_unit(self) -> None:
        requirement = "顺时针方向，扭矩为0.08～0.12N∙m 单位：N∙m"
        self.assertEqual(evaluate_numeric_measurement(requirement, "0.113")["status"], "pass")
        self.assertEqual(evaluate_numeric_measurement(requirement, "0.13")["status"], "error")
        self.assertEqual(evaluate_numeric_measurement("预置力矩应为 8.5±1.41N·cm。 单位：N·cm", "+0.58")["status"], "pass")


if __name__ == "__main__":
    unittest.main()
