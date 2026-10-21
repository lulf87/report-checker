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


if __name__ == "__main__":
    unittest.main()
