from fastapi.testclient import TestClient

from cycleflow.api import create_app
from cycleflow.contracts import DomainError
from cycleflow.demo import END, START, event
from tests.common import ServiceCase


def interleaved_events():
    """开工、领料、修订、完工、试验交错发生的完整时间线。

    J1/J2 在修订 R2 发布前开工领料;J2 在修订前完工;修订生效后登记
    处置,J1 转入新版、J2 沿用旧版,随后分别按新旧计划完成试验。
    """
    return [
        event(1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                       "revision": 1, "parts": {"motor": 2, "sensor": 1}}),
        event(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                    "supplier": "motors", "quantity": 12}),
        event(3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor",
                                    "supplier": "sensors", "quantity": 6}),
        event(4, "plan.create", 3, {"plan_id": "P1", "product": "robot-arm",
                                    "revision": 1, "minimum_pass_rate": "0.75"}),
        event(5, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 2}),
        event(6, "job.stage_start", 5, {"job_id": "J1", "stage": "machining"}),
        event(7, "job.stage_finish", 6, {"job_id": "J1", "stage": "machining"}),
        event(8, "job.open", 7, {"job_id": "J2", "design_id": "R1", "quantity": 2}),
        event(9, "job.stage_start", 8, {"job_id": "J2", "stage": "machining"}),
        event(10, "job.stage_finish", 9, {"job_id": "J2", "stage": "machining"}),
        event(11, "job.stage_start", 10, {"job_id": "J2", "stage": "assembly"}),
        event(12, "job.stage_finish", 11, {"job_id": "J2", "stage": "assembly"}),
        event(13, "job.stage_start", 12, {"job_id": "J2", "stage": "integration"}),
        event(14, "job.stage_finish", 13, {"job_id": "J2", "stage": "integration"}),
        event(15, "design.release", 14, {"design_id": "R2", "product": "robot-arm",
                                         "revision": 2, "parent_id": "R1",
                                         "parts": {"motor": 1, "sensor": 2}}),
        event(16, "plan.create", 15, {"plan_id": "P2", "product": "robot-arm",
                                      "revision": 2, "minimum_pass_rate": "0.5"}),
        event(17, "revision.effect", 16, {"design_id": "R2", "jobs": ["J1", "J2"],
                                          "note": "结构件改版生效"}),
        event(18, "revision.disposition", 17, {"job_id": "J1", "design_id": "R2",
                                               "decision": "upgrade",
                                               "reason": "结构件换新,转入新版验证"}),
        event(19, "revision.disposition", 18, {"job_id": "J2", "design_id": "R2",
                                               "decision": "continue",
                                               "reason": "已完工,按旧版交付"}),
        event(20, "job.stage_start", 19, {"job_id": "J1", "stage": "assembly"}),
        event(21, "job.stage_finish", 20, {"job_id": "J1", "stage": "assembly"}),
        event(22, "job.stage_start", 21, {"job_id": "J1", "stage": "integration"}),
        event(23, "job.stage_finish", 22, {"job_id": "J1", "stage": "integration"}),
        event(24, "test.record", 23, {"test_id": "T2", "job_id": "J2",
                                      "plan_id": "P1", "passed": 2, "failed": 0}),
        event(25, "test.record", 24, {"test_id": "T1", "job_id": "J1",
                                      "plan_id": "P2", "passed": 1, "failed": 1}),
    ]


class RevisionDispositionTests(ServiceCase):
    def run_events(self, events):
        outcomes = [self.service.ingest(item) for item in events]
        self.assertTrue(all(item["state"] == "accepted" for item in outcomes), outcomes)
        return outcomes

    def run_interleaved(self, count=None):
        events = interleaved_events()
        self.run_events(events if count is None else events[:count])
        return events

    def impact_entry(self, report, job_id):
        return {item["job_id"]: item for item in report["revision_impact"]["jobs"]}[job_id]

    def test_interleaved_flow_aggregates_by_effective_revision(self):
        self.run_interleaved()
        report = self.service.report("robot-arm", START, END)
        self.assertEqual(report["summary"]["job_count"], 2)
        self.assertEqual(report["summary"]["unit_count"], 4)
        self.assertEqual(report["summary"]["passed_units"], 3)
        groups = {item["revision"]: item for item in report["revisions"]}
        self.assertEqual(sorted(groups), [1, 2])
        self.assertEqual(sum(item["jobs"] for item in groups.values()), 2)
        self.assertEqual(groups[1]["jobs"], 1)
        self.assertEqual(groups[1]["passed_units"], 2)
        self.assertEqual(groups[2]["jobs"], 1)
        self.assertEqual(groups[2]["passed_units"], 1)
        self.assertEqual(groups[2]["pass_rate"], "0.5")
        jobs = {item["job_id"]: item for item in report["jobs"]}
        self.assertEqual(jobs["J1"]["revision"], 2)
        self.assertEqual(jobs["J1"]["opened_revision"], 1)
        self.assertEqual(jobs["J1"]["design_id"], "R1")
        self.assertEqual(jobs["J1"]["effective_design_id"], "R2")
        self.assertEqual(jobs["J1"]["tests"][0]["plan_id"], "P2")
        self.assertEqual(jobs["J2"]["revision"], 1)
        self.assertEqual(jobs["J2"]["tests"][0]["plan_id"], "P1")
        self.assertEqual({lot["lot_id"] for lot in jobs["J1"]["lots"]}, {"M1", "S1"})

    def test_report_explains_disposition_of_each_scoped_job(self):
        self.run_interleaved()
        report = self.service.report("robot-arm", START, END)
        impact = report["revision_impact"]
        self.assertEqual(len(impact["effects"]), 1)
        effect = impact["effects"][0]
        self.assertEqual(effect["design_id"], "R2")
        self.assertEqual(effect["revision"], 2)
        self.assertEqual(effect["scope_job_ids"], ["J1", "J2"])
        self.assertEqual(effect["source_event_id"], 17)
        self.assertEqual(effect["outcomes"], {"upgraded": 1, "continued": 1, "pending": 0})
        first = self.impact_entry(report, "J1")
        self.assertEqual(first["classification"], "upgraded")
        self.assertEqual(first["opened_revision"], 1)
        self.assertEqual(first["current_revision"], 2)
        self.assertEqual(first["reason"], "结构件换新,转入新版验证")
        self.assertEqual(first["decision_event_id"], 18)
        second = self.impact_entry(report, "J2")
        self.assertEqual(second["classification"], "continued")
        self.assertEqual(second["reason"], "已完工,按旧版交付")
        self.assertTrue(report["quality"]["publishable"])
        audit = self.service.audit("robot-arm")
        self.assertTrue(audit["ok"], audit)
        self.assertEqual(audit["finding_count"], 0)

    def test_lineage_survives_upgrade(self):
        self.run_interleaved()
        job = self.service.get_job("J1")
        self.assertEqual(job["design"]["design_id"], "R1")
        self.assertEqual(job["effective_design"]["design_id"], "R2")
        self.assertEqual(job["effective_design"]["parts"], {"motor": 1, "sensor": 2})
        self.assertEqual(len(job["stages"]), 3)
        self.assertEqual({row["lot_id"] for row in job["reservations"]}, {"M1", "S1"})
        self.assertEqual(len(job["dispositions"]), 1)
        self.assertEqual(job["active_disposition"]["decision"], "upgrade")
        trace = self.service.get_lineage(25)
        self.assertEqual(trace["trace"]["plan"]["plan_id"], "P2")
        self.assertEqual(trace["trace"]["job_lineage"][0]["design_id"], "R1")
        self.assertEqual({row["lot_id"] for row in trace["trace"]["reservations"]}, {"M1", "S1"})
        effect = self.service.get_effect("R2")
        self.assertEqual(effect["scope_job_ids"], ["J1", "J2"])
        self.assertEqual(len(effect["dispositions"]), 2)

    def test_test_plan_must_follow_disposition(self):
        events = self.run_interleaved(count=23)
        wrong_old = self.ingest(30, "test.record", 23, {"test_id": "T1", "job_id": "J1",
                                                        "plan_id": "P1", "passed": 2, "failed": 0})
        self.assertEqual(wrong_old["error_code"], "plan_mismatch")
        wrong_new = self.ingest(31, "test.record", 23, {"test_id": "T2", "job_id": "J2",
                                                        "plan_id": "P2", "passed": 2, "failed": 0})
        self.assertEqual(wrong_new["error_code"], "plan_mismatch")
        self.run_events(events[23:])
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self.assertEqual(self.service.get_job("J2")["status"], "completed")

    def test_withdraw_restores_original_and_allows_redecision(self):
        events = interleaved_events()
        self.run_events(events[:18])
        withdrawn = self.ingest(30, "revision.withdraw", 18,
                                {"job_id": "J1", "reason": "复评后暂不转版"})
        self.assertEqual(withdrawn["state"], "accepted")
        job = self.service.get_job("J1")
        self.assertEqual(job["effective_design"]["design_id"], "R1")
        self.assertIsNone(job["active_disposition"])
        self.assertIsNotNone(job["dispositions"][0]["withdrawn_at"])
        self.assertEqual(job["dispositions"][0]["withdraw_reason"], "复评后暂不转版")
        again = self.ingest(31, "revision.disposition", 19,
                            {"job_id": "J1", "design_id": "R2", "decision": "upgrade",
                             "reason": "再次评估后转入"})
        self.assertEqual(again["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["effective_design"]["design_id"], "R2")
        second = self.ingest(32, "revision.withdraw", 20, {"job_id": "J1"})
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["effective_design"]["design_id"], "R1")
        self.run_events(events[18:23])
        tested = self.ingest(33, "test.record", 23, {"test_id": "T1", "job_id": "J1",
                                                     "plan_id": "P1", "passed": 2, "failed": 0})
        self.assertEqual(tested["state"], "accepted")
        report = self.service.report("robot-arm", START, END)
        self.assertEqual({item["revision"] for item in report["revisions"]}, {1})
        entry = self.impact_entry(report, "J1")
        self.assertEqual(entry["classification"], "continued")
        self.assertEqual(len(entry["history"]), 2)
        self.assertTrue(all(item["withdrawn_at"] for item in entry["history"]))
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_withdrawal_rejected_once_job_completed(self):
        events = interleaved_events()
        self.run_events(events[:18])
        self.run_events(events[19:23])
        self.ingest(30, "test.record", 23, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P2", "passed": 2, "failed": 0})
        late = self.ingest(31, "revision.withdraw", 24, {"job_id": "J1"})
        self.assertEqual(late["state"], "rejected")
        self.assertEqual(late["error_code"], "terminal_job")
        self.assertEqual(self.service.get_job("J1")["active_disposition"]["decision"], "upgrade")

    def test_replay_keeps_projection_and_report_stable(self):
        events = self.run_interleaved()
        before = self.service.report("robot-arm", START, END)
        self.assertEqual(self.service.health()["event_head"], 25)
        for item in events:
            outcome = self.service.ingest(item)
            self.assertTrue(outcome["repeated"])
        self.assertEqual(self.service.health()["event_head"], 25)
        after = self.service.report("robot-arm", START, END)
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(len(self.service.get_job("J1")["dispositions"]), 1)
        self.assertEqual(len(self.service.get_job("J2")["dispositions"]), 1)

    def test_out_of_order_events_wait_then_retry(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.seed_job("J2", quantity=1, number=13, minute=7)
        early = self.ingest(10, "revision.disposition", 17,
                            {"job_id": "J1", "design_id": "R2", "decision": "upgrade",
                             "reason": "提前到达的处置"})
        self.assertEqual(early["state"], "waiting")
        self.assertEqual(early["error_code"], "missing_dependency")
        self.ingest(11, "design.release", 14, {"design_id": "R2", "product": "robot-arm",
                                               "revision": 2, "parent_id": "R1",
                                               "parts": {"motor": 1, "sensor": 2}})
        effect = self.ingest(12, "revision.effect", 16, {"design_id": "R2", "jobs": ["J1", "J2"]})
        self.assertEqual(effect["state"], "accepted")
        retried = self.service.retry(early["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["effective_design"]["design_id"], "R2")
        early_withdraw = self.ingest(14, "revision.withdraw", 19, {"job_id": "J2"})
        self.assertEqual(early_withdraw["state"], "waiting")
        decided = self.ingest(15, "revision.disposition", 18,
                              {"job_id": "J2", "design_id": "R2", "decision": "continue",
                               "reason": "留旧版交付"})
        self.assertEqual(decided["state"], "accepted")
        settled = self.service.retry(early_withdraw["id"])
        self.assertEqual(settled["state"], "accepted")
        job = self.service.get_job("J2")
        self.assertIsNone(job["active_disposition"])
        self.assertEqual(len(job["dispositions"]), 1)
        self.assertEqual(self.service.retry_waiting(), [])
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_effect_waits_for_design_and_job(self):
        pending = self.ingest(10, "revision.effect", 16, {"design_id": "R2", "jobs": ["J1"]})
        self.assertEqual(pending["state"], "waiting")
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.ingest(11, "design.release", 14, {"design_id": "R2", "product": "robot-arm",
                                               "revision": 2, "parent_id": "R1",
                                               "parts": {"motor": 1, "sensor": 2}})
        still = self.service.retry(pending["id"])
        self.assertEqual(still["state"], "waiting")
        self.assertEqual(still["error_code"], "missing_dependency")
        self.seed_job()
        done = self.service.retry(pending["id"])
        self.assertEqual(done["state"], "accepted")
        self.assertEqual(self.service.get_effect("R2")["scope_job_ids"], ["J1"])

    def test_terminal_job_cannot_be_scoped_or_dispositioned(self):
        self.full_scenario()
        self.ingest(20, "design.release", 31, {"design_id": "R2", "product": "robot-arm",
                                               "revision": 2, "parent_id": "R1",
                                               "parts": {"motor": 1, "sensor": 2}})
        scoped = self.ingest(21, "revision.effect", 32, {"design_id": "R2", "jobs": ["J1"]})
        self.assertEqual(scoped["state"], "rejected")
        self.assertEqual(scoped["error_code"], "terminal_job")
        decided = self.ingest(22, "revision.disposition", 33,
                              {"job_id": "J1", "design_id": "R2", "decision": "continue",
                               "reason": "试图补登"})
        self.assertEqual(decided["state"], "waiting")
        self.assertEqual(decided["error_code"], "missing_dependency")

    def test_terminal_job_rejects_disposition_after_scope(self):
        events = interleaved_events()
        self.run_events(events[:17])
        self.run_events(events[19:23])
        self.ingest(30, "test.record", 23, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 2, "failed": 0})
        late = self.ingest(31, "revision.disposition", 24,
                           {"job_id": "J1", "design_id": "R2", "decision": "continue",
                            "reason": "完工后补登"})
        self.assertEqual(late["state"], "rejected")
        self.assertEqual(late["error_code"], "terminal_job")

    def test_scope_and_decision_validation(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.ingest(11, "design.release", 14, {"design_id": "R2", "product": "robot-arm",
                                               "revision": 2, "parent_id": "R1",
                                               "parts": {"motor": 1, "sensor": 2}})
        empty = self.ingest(12, "revision.effect", 15, {"design_id": "R2", "jobs": []})
        self.assertEqual(empty["error_code"], "invalid_scope")
        doubled = self.ingest(13, "revision.effect", 15, {"design_id": "R2", "jobs": ["J1", "J1"]})
        self.assertEqual(doubled["error_code"], "duplicate_job")
        same = self.ingest(14, "revision.effect", 15, {"design_id": "R1", "jobs": ["J1"]})
        self.assertEqual(same["error_code"], "invalid_scope")
        registered = self.ingest(15, "revision.effect", 16, {"design_id": "R2", "jobs": ["J1"]})
        self.assertEqual(registered["state"], "accepted")
        again = self.ingest(16, "revision.effect", 17, {"design_id": "R2", "jobs": ["J1"]})
        self.assertEqual(again["error_code"], "duplicate_effect")
        bad = self.ingest(17, "revision.disposition", 18,
                          {"job_id": "J1", "design_id": "R2", "decision": "maybe", "reason": "x"})
        self.assertEqual(bad["error_code"], "invalid_decision")
        first = self.ingest(18, "revision.disposition", 18,
                            {"job_id": "J1", "design_id": "R2", "decision": "continue",
                             "reason": "留旧版"})
        self.assertEqual(first["state"], "accepted")
        second = self.ingest(19, "revision.disposition", 19,
                             {"job_id": "J1", "design_id": "R2", "decision": "upgrade",
                              "reason": "重复决定"})
        self.assertEqual(second["error_code"], "disposition_exists")

    def test_pending_disposition_blocks_publish_until_decided(self):
        events = interleaved_events()
        self.run_events(events[:17])
        report = self.service.report("robot-arm", START, END)
        pending = {item["subject"] for item in report["quality"]["issues"]
                   if item["code"] == "pending_disposition"}
        self.assertEqual(pending, {"J1", "J2"})
        self.assertFalse(report["quality"]["publishable"])
        with self.assertRaises(DomainError) as caught:
            self.service.publish("rev-jan", "robot-arm", START, END, report["fingerprint"])
        self.assertEqual(caught.exception.code, "quality_block")
        self.run_events(events[17:])
        ready = self.service.report("robot-arm", START, END)
        self.assertTrue(ready["quality"]["publishable"])
        frozen = self.service.publish("rev-jan", "robot-arm", START, END, ready["fingerprint"])
        outcomes = frozen["report"]["revision_impact"]["effects"][0]["outcomes"]
        self.assertEqual(outcomes, {"upgraded": 1, "continued": 1, "pending": 0})

    def test_frozen_release_reproduces_original_snapshot(self):
        report = self.full_scenario()
        frozen = self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        self.ingest(20, "lot.receive", 31, {"lot_id": "M2", "part": "motor",
                                            "supplier": "motors", "quantity": 4})
        self.ingest(21, "lot.receive", 32, {"lot_id": "S2", "part": "sensor",
                                            "supplier": "sensors", "quantity": 2})
        self.ingest(22, "design.release", 33, {"design_id": "R2", "product": "robot-arm",
                                               "revision": 2, "parent_id": "R1",
                                               "parts": {"motor": 1, "sensor": 2}})
        self.ingest(23, "job.open", 34, {"job_id": "J2", "design_id": "R1", "quantity": 1})
        self.ingest(24, "revision.effect", 35, {"design_id": "R2", "jobs": ["J2"]})
        self.ingest(25, "revision.disposition", 36,
                    {"job_id": "J2", "design_id": "R2", "decision": "upgrade",
                     "reason": "转入新版"})
        same = self.service.get_release("jan")
        self.assertEqual(same["fingerprint"], frozen["fingerprint"])
        self.assertEqual(same["report"]["revision_impact"]["jobs"], [])
        self.assertEqual({item["revision"] for item in same["report"]["revisions"]}, {1})
        freshness = self.service.shelf.freshness("jan")
        self.assertTrue(freshness["current_differs"])
        current = self.service.report("robot-arm", START, END)
        jobs = {item["job_id"]: item for item in current["jobs"]}
        self.assertEqual(jobs["J1"]["revision"], 1)
        self.assertEqual(jobs["J2"]["revision"], 2)
        self.assertEqual(jobs["J2"]["opened_revision"], 1)

    def test_http_effect_and_job_views(self):
        client = TestClient(create_app(str(self.path)))
        for item in interleaved_events():
            response = client.post("/events", json=item)
            self.assertEqual(response.status_code, 200, response.text)
        effect = client.get("/effects/R2")
        self.assertEqual(effect.status_code, 200)
        self.assertEqual(effect.json()["scope_job_ids"], ["J1", "J2"])
        job = client.get("/jobs/J1")
        self.assertEqual(job.json()["design"]["design_id"], "R1")
        self.assertEqual(job.json()["effective_design"]["design_id"], "R2")
        report = client.get("/reports/robot-arm", params={"window_start": START, "window_end": END})
        impact = report.json()["revision_impact"]
        self.assertEqual(impact["effects"][0]["outcomes"]["upgraded"], 1)
        self.assertTrue(client.get("/quality/audit").json()["ok"])
