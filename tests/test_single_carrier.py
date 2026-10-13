"""When no reply address maps to a WAN but only one WAN carries traffic, that WAN is named as the one that carried it."""
import unittest

import app as netem


def link(link_id, down=0.0, up=0.0):
    return {"link_id": link_id, "label": link_id.upper(), "causes": [],
            "injected": {"delay_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 0.0},
            "directions": {"down": {"rate_mbps": down}, "up": {"rate_mbps": up}}}


VOICE_LOSS = {"id": "media_loss", "source": "experience", "severity": "warn", "title": "Voice: bursts lost packets",
              "applications": ["voice"], "by_egress": {"unknown": {"requests": 4}}}


class SingleCarrierTests(unittest.TestCase):
    def test_the_only_wan_carrying_traffic_is_named_when_no_address_maps(self):
        findings = netem.correlate_findings({"findings": [VOICE_LOSS]}, [link("wan1", down=0.9, up=7.4), link("wan2")], {})
        wans = [wan for item in findings for wan in item["wans"]]
        self.assertEqual([(wan["link_id"], wan["affected"]) for wan in wans], [("wan1", 4)])

    def test_several_carriers_leave_the_impact_unidentified(self):
        findings = netem.correlate_findings({"findings": [VOICE_LOSS]},
                                            [link("wan1", down=5.0), link("wan2", up=3.0)], {})
        self.assertEqual([wan for item in findings for wan in item["wans"]], [])
        self.assertEqual(findings[0]["unattributed"], 4)

    def test_no_carrier_leaves_the_impact_unidentified(self):
        self.assertIsNone(netem.single_carrier([link("wan1"), link("wan2")]))


if __name__ == "__main__":
    unittest.main()
