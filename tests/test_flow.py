import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(2, len(opened["bids"]))
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)


class ConsortiumFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.lead = self.service.create_vendor("proc1", "procurement", "V-101", "联合体牵头", "lead1")
        self.member = self.service.create_vendor("proc1", "procurement", "V-102", "联合体成员", "member1")
        self.alt = self.service.create_vendor("proc1", "procurement", "V-103", "替补成员", "alt1")
        self.solo = self.service.create_vendor("proc1", "procurement", "V-104", "独立供应商", "solo1")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.lead_qual = self.service.register_qualification("proc1", "procurement", self.lead["id"], "CERT-L1", "质量管理体系", future)
        self.member_qual = self.service.register_qualification("proc1", "procurement", self.member["id"], "CERT-M1", "安全生产许可", future)
        self.alt_qual = self.service.register_qualification("proc1", "procurement", self.alt["id"], "CERT-A1", "信息安全资质", future)
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-CONS", "联合体项目", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def consortium(self, members=None, no="C-001"):
        members = members or [{"vendor_id": self.lead["id"], "share": 60}, {"vendor_id": self.member["id"], "share": 40}]
        return self.service.create_consortium("lead1", "vendor", no, "测试联合体", self.lead["id"], members)

    def bid(self, consortium, price=800000, quality=90):
        return self.service.submit_bid("lead1", "vendor", self.tender["id"], self.lead["id"],
                                       {"报价": price, "质量": quality}, price, consortium_id=consortium["id"])

    def test_version_snapshot_frozen_and_duplicate_create_keeps_one(self):
        cons = self.consortium()
        self.assertEqual(1, len(cons["versions"]))
        snap = cons["versions"][0]
        self.assertEqual(1, snap["version"])
        self.assertEqual("active", snap["status"])
        self.assertTrue(snap["snapshot_hash"])
        shares = {m["vendor_id"]: m["share"] for m in snap["members"]}
        self.assertEqual({self.lead["id"]: 60, self.member["id"]: 40}, shares)
        member_snap = [m for m in snap["members"] if m["vendor_id"] == self.member["id"]][0]
        self.assertEqual("CERT-M1", member_snap["qualifications"][0]["cert_no"])
        self.assertEqual("valid", member_snap["qualifications"][0]["current_status"])
        again = self.consortium()
        self.assertEqual(cons["id"], again["id"])
        self.assertEqual(1, len(again["versions"]))
        with self.assertRaises(DomainError) as ctx:
            self.consortium(members=[{"vendor_id": self.lead["id"], "share": 100}])
        self.assertEqual(409, ctx.exception.status)

    def test_member_change_new_version_unopened_bid_invalid_and_hash_kept(self):
        cons = self.consortium()
        bid = self.bid(cons)
        self.assertEqual(cons["versions"][0]["id"], bid["consortium_version_id"])
        dup = self.bid(cons)
        self.assertEqual(bid["id"], dup["id"])
        revised = self.service.revise_consortium(
            "lead1", "vendor", cons["id"],
            [{"vendor_id": self.lead["id"], "share": 60}, {"vendor_id": self.alt["id"], "share": 40}],
            "原成员退出",
        )
        versions = {v["version"]: v for v in revised["versions"]}
        self.assertEqual("superseded", versions[1]["status"])
        self.assertEqual("active", versions[2]["status"])
        self.assertNotEqual(versions[1]["snapshot_hash"], versions[2]["snapshot_hash"])
        after = [b for b in self.service.state("sup1", "supervisor")["bids"] if b["id"] == bid["id"]][0]
        self.assertEqual("invalid", after["status"])
        self.assertEqual(bid["payload_hash"], after["payload_hash"])
        self.assertEqual(bid["id"], versions[1]["affected_bids"][0]["id"])
        resub = self.bid(cons, price=780000, quality=92)
        self.assertEqual(bid["id"], resub["id"])
        self.assertEqual("sealed", resub["status"])
        self.assertEqual(versions[2]["id"], resub["consortium_version_id"])

    def test_opened_bid_blocked_after_qualification_suspension(self):
        cons = self.consortium()
        cbid = self.bid(cons)
        sbid = self.service.submit_bid("solo1", "vendor", self.tender["id"], self.solo["id"],
                                       {"报价": 700000, "质量": 85}, 700000)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.set_qualification_status("proc1", "procurement", self.member_qual["id"], "suspended", "年检未通过")
        detail = self.service.get_consortium("aud1", "auditor", cons["id"])
        self.assertEqual("invalid", detail["versions"][0]["status"])
        self.assertEqual("成员证书暂停：CERT-M1", detail["versions"][0]["invalid_reason"])
        bids = {b["id"]: b for b in self.service.get_tender("sup1", "supervisor", self.tender["id"])["bids"]}
        self.assertEqual("opened", bids[cbid["id"]]["status"])
        self.assertEqual("invalid", bids[cbid["id"]]["consortium_version_status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", cbid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx.exception.status)
        self.service.evaluate_bid("eval1", "evaluator", sbid["id"], {"报价": 700000, "质量": 85})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])
        self.assertEqual(sbid["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(cbid["id"], award["award"]["excluded_bids"][0]["bid_id"])

    def test_lead_change_resubmit_keeps_single_bid_row(self):
        cons = self.consortium()
        bid = self.bid(cons)
        revised = self.service.revise_consortium(
            "lead1", "vendor", cons["id"],
            [{"vendor_id": self.member["id"], "share": 60}, {"vendor_id": self.alt["id"], "share": 40}],
            "牵头方退出", lead_vendor_id=self.member["id"],
        )
        self.assertEqual(self.member["id"], revised["lead_vendor_id"])
        resub = self.service.submit_bid("member1", "vendor", self.tender["id"], self.member["id"],
                                        {"报价": 760000, "质量": 91}, 760000, consortium_id=cons["id"])
        self.assertEqual(bid["id"], resub["id"])
        self.assertEqual("sealed", resub["status"])
        self.assertEqual(self.member["id"], resub["vendor_id"])
        bids = [b for b in self.service.state("sup1", "supervisor")["bids"] if b["tender_id"] == self.tender["id"]]
        self.assertEqual(1, len(bids))
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_bid("lead1", "vendor", self.tender["id"], self.lead["id"],
                                    {"报价": 800000, "质量": 90}, 800000, consortium_id=cons["id"])
        self.assertEqual(403, ctx.exception.status)

    def test_certificate_expiry_invalidates_version_on_sweep(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.service.register_qualification("proc1", "procurement", self.alt["id"], "CERT-TMP", "临时证书", soon)
        cons = self.service.create_consortium(
            "lead1", "vendor", "C-EXP", "过期测试联合体", self.lead["id"],
            [{"vendor_id": self.lead["id"], "share": 50}, {"vendor_id": self.alt["id"], "share": 50}],
        )
        bid = self.bid(cons)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(0, len(opened["bids"]))
        detail = self.service.get_consortium("aud1", "auditor", cons["id"])
        self.assertEqual("invalid", detail["versions"][0]["status"])
        self.assertIn("过期", detail["versions"][0]["invalid_reason"])
        bids = self.service.state("sup1", "supervisor")["bids"]
        self.assertEqual("invalid", [b for b in bids if b["id"] == bid["id"]][0]["status"])


if __name__ == "__main__":
    unittest.main()
