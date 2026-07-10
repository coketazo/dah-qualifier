from __future__ import annotations

import unittest

from common.geo import haversine_m, offset_m
from mock_gcs.autopilot import Autopilot, MISSION_COMPROMISE_M


class TargetModelTests(unittest.TestCase):
    def test_normal_long_run_keeps_independent_nav_bounded(self) -> None:
        """30분 상당 정상운용에서 예전의 333초 고정 오탐이 재발하지 않는다."""
        ap = Autopilot()
        peak = 0.0
        for _ in range(int(30 * 60 / 0.2)):
            ap.set_operator_heartbeat()
            ap.step(0.2)
            peak = max(peak, haversine_m(ap.gps_meas, ap.extnav_pos))
        self.assertLess(peak, 10.0)
        self.assertFalse(ap.truth()["mission_compromised"])

    def test_mission_compromise_uses_operational_threshold(self) -> None:
        ap = Autopilot()
        ap.ekf_pos = offset_m(ap.true_pos, 0.0, MISSION_COMPROMISE_M - 1.0)
        self.assertFalse(ap.truth()["mission_compromised"])
        ap.ekf_pos = offset_m(ap.true_pos, 0.0, MISSION_COMPROMISE_M + 1.0)
        self.assertTrue(ap.truth()["mission_compromised"])

    def test_mitigation_enters_external_nav_recovery_rtl(self) -> None:
        ap = Autopilot()
        ap.attack_observed = True
        ap.mitigate("test")
        self.assertEqual(ap.truth()["defense_state"], "GNSS_QUARANTINED")
        for _ in range(11):
            ap.step(0.2)
        t = ap.truth()
        self.assertEqual(t["defense_state"], "EXTERNAL_NAV_RTL")
        self.assertEqual(t["nav_source"], "EXTERNAL_NAV")
        self.assertEqual(t["mode"], "RTL")
        self.assertEqual(t["platform_availability"], 100)

    def test_telemetry_declares_external_nav_source(self) -> None:
        ap = Autopilot()
        messages = {m.get_type(): m for m in ap.telemetry_messages()}
        self.assertIn("ODOMETRY", messages)
        self.assertEqual(messages["ODOMETRY"].estimator_type, 3)  # MAV_ESTIMATOR_TYPE_VIO
        self.assertEqual(messages["ODOMETRY"].quality, 90)

    def test_safe_hold_does_not_rtl_on_degraded_external_nav(self) -> None:
        """독립 ExternalNav 품질 불충분 시 대체 대응: RTL 하지 않고 안전 고정."""
        ap = Autopilot()
        ap.attack_observed = True
        ap.degrade_external_nav(30.0)
        ap.safe_hold("test")
        for _ in range(15):
            ap.step(0.2)
        t = ap.truth()
        self.assertEqual(t["defense_state"], "SAFE_HOLD")
        self.assertEqual(t["nav_source"], "INERTIAL_HOLD")
        self.assertEqual(t["mode"], "LOITER")          # 자동복귀(RTL) 하지 않음
        self.assertEqual(t["external_nav_sigma_m"], 30.0)
        self.assertFalse(t["mission_compromised"])

    def test_degrade_external_nav_raises_odometry_covariance(self) -> None:
        ap = Autopilot()
        ap.degrade_external_nav(25.0)
        odo = {m.get_type(): m for m in ap.telemetry_messages()}["ODOMETRY"]
        self.assertAlmostEqual(odo.pose_covariance[0], 625.0)  # sigma^2 = 25^2

    def test_c2_reject_counters_distinguish_sensor_injection(self) -> None:
        ap = Autopilot()
        ap.record_c2_reject(sensor_message=False)
        ap.record_c2_reject(sensor_message=True)
        t = ap.truth()
        self.assertEqual(t["rejected_unsigned"], 2)
        self.assertEqual(t["rejected_c2_sensor"], 1)


if __name__ == "__main__":
    unittest.main()
