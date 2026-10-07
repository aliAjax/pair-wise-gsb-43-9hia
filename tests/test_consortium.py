import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ConsortiumVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.v1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "lead")
        self.v2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "partner-a")
        self.v3 = self.service.create_vendor("proc1", "procurement", "V-103", "瀚海集成", "partner-b")
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.service.register_qualification("proc1", "procurement", self.v1["id"], "CERT-1", "集成", future)
        self.service.register_qualification("proc1", "procurement", self.v2["id"], "CERT-2", "安防", future)
        self.service.register_qualification("proc1", "procurement", self.v3["id"], "CERT-3", "弱电", future)
        self.future, self.past = future, past
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-201", "智慧园区",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria,
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, no="CONS-1", lead=None, members=None):
        lead = lead or self.v1["id"]
        members = members if members is not None else [
            {"vendor_id": self.v1["id"], "share": 60},
            {"vendor_id": self.v2["id"], "share": 40},
        ]
        return self.service.register_consortium("lead", "vendor", no, lead, members)

    def test_submit_freezes_lead_shares_and_qualification_snapshot(self):
        c = self.register()
        self.assertEqual(1, c["version"])
        self.assertEqual("active", c["status"])
        self.assertTrue(c["effective"])
        self.assertEqual(self.v1["id"], c["lead_vendor_id"])
        shares = {m["vendor_id"]: m["share"] for m in c["members"]}
        self.assertEqual({self.v1["id"]: 60.0, self.v2["id"]: 40.0}, shares)
        for member in c["members"]:
            self.assertEqual("valid", member["qualification_status"])
            self.assertTrue(member["frozen"]["frozen_cert_valid"])
            self.assertTrue(member["frozen"]["cert_no"])
        self.assertEqual(64, len(c["snapshot_hash"]))

    def test_shares_must_total_100_and_lead_must_be_member(self):
        with self.assertRaises(DomainError):
            self.register(members=[{"vendor_id": self.v1["id"], "share": 70},
                                   {"vendor_id": self.v2["id"], "share": 20}])
        with self.assertRaises(DomainError):
            self.register(lead=self.v3["id"], members=[
                {"vendor_id": self.v1["id"], "share": 60},
                {"vendor_id": self.v2["id"], "share": 40},
            ])

    def test_duplicate_submission_keeps_single_version(self):
        first = self.register()
        second = self.register()
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["id"], second["id"])
        rows = self.service.list_consortia("proc1", "procurement", "CONS-1")["consortia"]
        self.assertEqual(1, len(rows))

    def test_member_change_creates_new_version_and_invalidates_sealed_bid(self):
        c1 = self.register()
        bid = self.service.submit_bid(
            "lead", "vendor", self.tender["id"], self.v1["id"],
            {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
        )
        original_hash = bid["payload_hash"]
        c2 = self.register(members=[
            {"vendor_id": self.v1["id"], "share": 70},
            {"vendor_id": self.v3["id"], "share": 30},
        ])
        self.assertEqual(2, c2["version"])
        self.assertFalse(c2["deduplicated"])
        old = self.service.get_consortium("proc1", "procurement", c1["id"])
        self.assertEqual("superseded", old["status"])
        self.assertFalse(old["effective"])
        self.assertEqual(c2["id"], c2.get("id"))
        stale_bid = self.service.get_tender("proc1", "procurement", self.tender["id"])["bids"]
        record = next(b for b in stale_bid if b["id"] == bid["id"])
        self.assertEqual("invalid", record["status"])
        self.assertEqual(original_hash, record["payload_hash"])
        self.assertFalse(record["consortium"]["effective"])

    def test_opened_bid_keeps_snapshot_but_scoring_and_award_blocked(self):
        c1 = self.register()
        bid = self.service.submit_bid(
            "lead", "vendor", self.tender["id"], self.v1["id"],
            {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
        )
        other = self.service.submit_bid(
            "partner-b", "vendor", self.tender["id"], self.v3["id"],
            {"报价": 750000, "质量": 85}, 750000,
        )
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(2, len(opened["bids"]))
        self.register(members=[
            {"vendor_id": self.v1["id"], "share": 80},
            {"vendor_id": self.v3["id"], "share": 20},
        ])
        view = self.service.get_tender("proc1", "procurement", self.tender["id"])
        consortium_bid = next(b for b in view["bids"] if b["id"] == bid["id"])
        self.assertEqual("opened", consortium_bid["status"])
        self.assertIn("payload", consortium_bid)
        self.assertFalse(consortium_bid["consortium"]["effective"])
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx.exception.status)
        self.service.evaluate_bid("eval1", "evaluator", other["id"], {"报价": 750000, "质量": 85})
        self.service.evaluate_bid("eval2", "evaluator", other["id"], {"报价": 750000, "质量": 85})
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"],
                                          self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["version"])
        self.assertEqual(other["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(1, len(award["award"]["ranking"]))
        blocked = award["award"]["blocked_consortium_bids"]
        self.assertEqual(bid["id"], blocked[0]["bid_id"])

    def test_member_suspension_invalidates_original_version(self):
        c1 = self.register()
        bid = self.service.submit_bid(
            "lead", "vendor", self.tender["id"], self.v1["id"],
            {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
        )
        self.service.set_qualification_suspension("proc1", "procurement", self.v2["id"], True, "处罚")
        view = self.service.get_consortium("proc1", "procurement", c1["id"])
        self.assertFalse(view["effective"])
        self.assertIn("暂停", view["invalid_reason"])
        member = next(m for m in view["members"] if m["vendor_id"] == self.v2["id"])
        self.assertEqual("suspended", member["qualification_status"])
        self.assertFalse(member["frozen"]["frozen_suspended"])
        record = self.service.get_tender("proc1", "procurement", self.tender["id"])["bids"]
        self.assertEqual("invalid", next(b for b in record if b["id"] == bid["id"])["status"])
        with self.assertRaises(DomainError):
            self.service.submit_bid(
                "lead", "vendor", self.tender["id"], self.v1["id"],
                {"报价": 790000, "质量": 91}, 790000, consortium_id=c1["id"],
            )
        self.service.set_qualification_suspension("proc1", "procurement", self.v2["id"], False, "解除")
        self.assertTrue(self.service.get_consortium("proc1", "procurement", c1["id"])["effective"])

    def test_expired_certificate_blocks_at_open(self):
        self.service.register_qualification("proc1", "procurement", self.v2["id"], "CERT-2", "安防", self.past)
        c1 = self.register(no="CONS-EXPIRED")
        self.assertFalse(c1["effective"])
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_bid(
                "lead", "vendor", self.tender["id"], self.v1["id"],
                {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
            )
        self.assertEqual(409, ctx.exception.status)
        c2 = self.register(no="CONS-LIVE", members=[
            {"vendor_id": self.v1["id"], "share": 70},
            {"vendor_id": self.v3["id"], "share": 30},
        ])
        bid = self.service.submit_bid(
            "lead", "vendor", self.tender["id"], self.v1["id"],
            {"报价": 800000, "质量": 90}, 800000, consortium_id=c2["id"],
        )
        self.service.register_qualification("proc1", "procurement", self.v3["id"], "CERT-3", "弱电", self.past)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(0, len(opened["bids"]))
        self.assertEqual(bid["id"], opened["blocked_bids"][0]["bid_id"])
        view = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("invalid", next(b for b in view["bids"] if b["id"] == bid["id"])["status"])

    def test_non_lead_cannot_submit_consortium_bid(self):
        c1 = self.register()
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_bid(
                "partner-a", "vendor", self.tender["id"], self.v2["id"],
                {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
            )
        self.assertEqual(403, ctx.exception.status)

    def test_audit_and_page_data_show_versions_members_and_affected_bids(self):
        c1 = self.register()
        bid = self.service.submit_bid(
            "lead", "vendor", self.tender["id"], self.v1["id"],
            {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
        )
        state = self.service.state("proc1", "procurement")
        entry = next(c for c in state["consortia"] if c["id"] == c1["id"])
        self.assertEqual(2, len(entry["members"]))
        affected_ids = {b["id"] for b in entry["affected_bids"]}
        self.assertIn(bid["id"], affected_ids)
        bid_view = next(b for b in state["bids"] if b["id"] == bid["id"])
        self.assertEqual("CONS-1", bid_view["consortium"]["consortium_no"])
        self.assertEqual(1, bid_view["consortium"]["version"])
        actions = [event["action"] for event in state["timeline"]]
        self.assertIn("consortium.version_frozen", actions)
        self.assertIn("bid.submitted", actions)

    def test_snapshot_hash_immutable_across_member_changes(self):
        c1 = self.register()
        self.service.submit_bid(
            "lead", "vendor", self.tender["id"], self.v1["id"],
            {"报价": 800000, "质量": 90}, 800000, consortium_id=c1["id"],
        )
        self.register(members=[
            {"vendor_id": self.v1["id"], "share": 50},
            {"vendor_id": self.v2["id"], "share": 50},
        ])
        old = self.service.get_consortium("proc1", "procurement", c1["id"])
        import hashlib
        import json
        raw = json.dumps(old["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), old["snapshot_hash"])
        bid = self.service.get_tender("proc1", "procurement", self.tender["id"])["bids"][0]
        self.assertEqual(old["snapshot_hash"], bid["consortium"]["snapshot_hash"])


if __name__ == "__main__":
    unittest.main()
