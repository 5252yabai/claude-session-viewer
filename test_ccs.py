"""ccs 테스트. 실행: python3 -m unittest test_ccs -v"""

import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

_spec = importlib.util.spec_from_loader(
    "ccs",
    importlib.machinery.SourceFileLoader(
        "ccs", os.path.join(os.path.dirname(__file__), "ccs")
    ),
)
ccs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccs)


def sess(**kw):
    base = {
        "id": "x", "path": "/p", "project": "proj", "size": 1,
        "mtime": 0.0, "title": "t", "cwd": "/Users/u/proj",
        "branch": "", "started": "2026-08-01T00:00:00Z",
    }
    base.update(kw)
    return base


class GroupSessions(unittest.TestCase):
    def test_groups_sessions_by_cwd(self):
        a1 = sess(id="a1", cwd="/Users/u/alpha")
        b1 = sess(id="b1", cwd="/Users/u/beta")
        a2 = sess(id="a2", cwd="/Users/u/alpha")
        groups = ccs.group_sessions([a1, b1, a2])
        self.assertEqual(
            [[s["id"] for s in g["sessions"]] for g in groups],
            [["a1", "a2"], ["b1"]],
        )

    def test_orders_groups_by_latest_session_desc(self):
        b_old = sess(id="b", cwd="/Users/u/beta", started="2026-07-01T00:00:00Z")
        a_new = sess(id="a", cwd="/Users/u/alpha", started="2026-08-02T00:00:00Z")
        groups = ccs.group_sessions([b_old, a_new])
        self.assertEqual(
            [g["sessions"][0]["id"] for g in groups], ["a", "b"]
        )

    def test_label_abbreviates_home_to_tilde(self):
        with tempfile.TemporaryDirectory() as tmp, fake_home(tmp):
            s = sess(cwd=tmp + "/texts/wiki")
            self.assertEqual(ccs.group_sessions([s])[0]["label"], "~/texts/wiki")

    def test_label_keeps_path_outside_home(self):
        s = sess(cwd="/opt/work")
        self.assertEqual(ccs.group_sessions([s])[0]["label"], "/opt/work")

    def test_label_falls_back_to_project_when_no_cwd(self):
        s = sess(cwd="", project="-Users-u-ghost")
        self.assertEqual(ccs.group_sessions([s])[0]["label"], "-Users-u-ghost")


@contextlib.contextmanager
def fake_home(path):
    old = ccs.HOME
    ccs.HOME = path
    try:
        yield
    finally:
        ccs.HOME = old


def git_repo(path):
    os.makedirs(os.path.join(path, ".git"))
    return path


def git_worktree(path, repo, name):
    os.makedirs(path)
    with open(os.path.join(path, ".git"), "w") as f:
        f.write("gitdir: %s/.git/worktrees/%s\n" % (repo, name))
    return path


class RepoGrouping(unittest.TestCase):
    def test_worktree_session_joins_main_repo_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(os.path.join(tmp, "repo"))
            wt = git_worktree(os.path.join(tmp, "wt1"), repo, "wt1")
            main, side = sess(id="m", cwd=repo), sess(id="w", cwd=wt)
            groups = ccs.group_sessions([main, side])
        self.assertEqual(
            [[s["id"] for s in g["sessions"]] for g in groups], [["m", "w"]]
        )
        self.assertEqual(main["wt"], "")
        self.assertEqual(side["wt"], "wt1")

    def test_resolves_from_subdirectory_of_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(os.path.join(tmp, "repo"))
            wt = git_worktree(os.path.join(tmp, "wt1"), repo, "wt1")
            sub = os.path.join(wt, "packages", "app")
            os.makedirs(sub)
            groups = ccs.group_sessions(
                [sess(id="m", cwd=repo), sess(id="w", cwd=sub)]
            )
        self.assertEqual(len(groups), 1)

    def test_deleted_worktree_resolved_via_repo_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(os.path.join(tmp, "repo"))
            gone = os.path.join(tmp, "gone")  # 디스크에 없음
            meta = os.path.join(repo, ".git", "worktrees", "gone")
            os.makedirs(meta)
            with open(os.path.join(meta, "gitdir"), "w") as f:
                f.write(gone + "/.git\n")
            main, side = sess(id="m", cwd=repo), sess(id="w", cwd=gone)
            groups = ccs.group_sessions([main, side])
        self.assertEqual(len(groups), 1)
        self.assertEqual(side["wt"], "gone")

    def test_claude_worktrees_path_heuristic_without_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app")  # .git 없음 → 휴리스틱만으로 병합
            groups = ccs.group_sessions(
                [
                    sess(id="m", cwd=app),
                    sess(id="w", cwd=app + "/.claude/worktrees/foo"),
                ]
            )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["sessions"][1]["wt"], "foo")

    def test_wt_dir_heuristic_matches_longest_repo_basename(self):
        with tempfile.TemporaryDirectory() as tmp, fake_home(tmp):
            web = git_repo(os.path.join(tmp, "web"))
            web2 = git_repo(os.path.join(tmp, "web-2"))
            orphan = sess(id="o", cwd=tmp + "/.wt/web-2-fix")  # 디스크에 없음
            groups = ccs.group_sessions(
                [sess(id="a", cwd=web), sess(id="b", cwd=web2), orphan]
            )
        by_label = {
            g["label"]: [s["id"] for s in g["sessions"]] for g in groups
        }
        self.assertEqual(by_label["~/web-2"], ["b", "o"])
        self.assertEqual(by_label["~/web"], ["a"])
        self.assertEqual(orphan["wt"], "web-2-fix")

    def test_unresolvable_cwd_stays_standalone_group(self):
        groups = ccs.group_sessions(
            [sess(id="a", cwd="/opt/alpha"), sess(id="b", cwd="/opt/beta")]
        )
        self.assertEqual(len(groups), 2)


_CACHE_TMP = None


def setUpModule():
    """스캔 캐시를 사용자 홈 밖으로 돌린다 — 테스트가 실제 캐시를 건드리지 않게."""
    global _CACHE_TMP
    _CACHE_TMP = tempfile.TemporaryDirectory()
    os.environ["XDG_CACHE_HOME"] = _CACHE_TMP.name


def tearDownModule():
    os.environ.pop("XDG_CACHE_HOME", None)
    _CACHE_TMP.cleanup()


def cmd_prompt(name, args=""):
    """슬래시 커맨드 프롬프트의 원문 형태."""
    return (
        "<command-message>%s</command-message>"
        "<command-name>%s</command-name>"
        "<command-args>%s</command-args>" % (name.lstrip("/"), name, args)
    )


def write_jsonl(dirpath, sid, cwd, title, ts, extra=(), ai_titles=(), pr=0):
    os.makedirs(dirpath, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": ts, "cwd": cwd,
         "message": {"role": "user", "content": t}}
        for t in [title] + list(extra)
    ]
    lines += [{"type": "ai-title", "aiTitle": t, "sessionId": sid} for t in ai_titles]
    if pr:
        lines.append({"type": "pr-link", "sessionId": sid, "prNumber": pr,
                      "prUrl": "https://github.com/o/r/pull/%d" % pr})
    with open(os.path.join(dirpath, sid + ".jsonl"), "w") as f:
        f.write("".join(json.dumps(x) + "\n" for x in lines))


def render_index(dirs):
    h = ccs.Handler.__new__(ccs.Handler)
    h.dirs = dirs
    return ccs.Handler.index(h)


def one(dirpath, **kw):
    """세션 하나를 쓰고 scan_sessions 요약 dict 를 돌려준다."""
    base = {"sid": "s1", "cwd": "/opt/x", "title": "제목",
            "ts": "2026-08-01T00:00:00Z"}
    base.update(kw)
    write_jsonl(dirpath, base.pop("sid"), base.pop("cwd"), base.pop("title"),
                base.pop("ts"), **base)
    return ccs.scan_sessions([dirpath])[0]


class SessionTitle(unittest.TestCase):
    def test_ai_title_beats_first_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title=cmd_prompt("/model", "opus"),
                    ai_titles=["API 교체 필요 페이지 분류 문서 작성"])
        self.assertEqual(s["title"], "API 교체 필요 페이지 분류 문서 작성")

    def test_last_ai_title_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, ai_titles=["첫 제목", "고쳐 쓴 제목"])
        self.assertEqual(s["title"], "고쳐 쓴 제목")

    def test_falls_back_to_prompt_without_ai_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title="토글 열고 닫을 때 헤더가 깜빡인다")
        self.assertEqual(s["title"], "토글 열고 닫을 때 헤더가 깜빡인다")

    def test_slash_command_becomes_badge_not_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title=cmd_prompt("/mattpocock-skills:wayfinder", "지도 그리기"))
        self.assertEqual(s["badge"], "wayfinder")
        self.assertEqual(s["title"], "지도 그리기")

    def test_at_path_argument_keeps_basename_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title=cmd_prompt("/wayfinder", "@/private/tmp/x/handoff.md 이어서"))
        self.assertEqual(s["title"], "@handoff.md 이어서")

    def test_image_reference_dropped_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title="[Image #3] [Image #4] 토글 헤더 깜빡임 수정")
        self.assertEqual(s["title"], "토글 헤더 깜빡임 수정")

    def test_control_command_skips_to_next_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title=cmd_prompt("/clear"), extra=["세션 뷰어 제목 고치기"])
        self.assertEqual(s["title"], "세션 뷰어 제목 고치기")
        self.assertEqual(s["badge"], "")

    def test_badge_survives_when_ai_title_supplies_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title=cmd_prompt("/pr-review", "26793"), ai_titles=["PR 리뷰 정리"])
        self.assertEqual((s["badge"], s["title"]), ("pr-review", "PR 리뷰 정리"))

    def test_no_material_yields_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title=cmd_prompt("/clear"))
        self.assertEqual(s["title"], "(제목 없음)")

    def test_absolute_path_prompt_is_not_mistaken_for_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title="/Users/u/Desktop/plan.md 기반으로 코드 생성")
        self.assertEqual(s["title"], "/Users/u/Desktop/plan.md 기반으로 코드 생성")
        self.assertEqual(s["turns"], 1)

    def test_export_masks_home_path_in_title(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp, fake_home("/Users/u"):
            write_jsonl(tmp, "abc123", "/opt/x", "/Users/u/secret/plan.md 정리",
                        "2026-08-01T00:00:00Z")
            out = os.path.join(tmp, "o.html")
            with contextlib.redirect_stdout(io.StringIO()):
                ccs.cmd_export(argparse.Namespace(
                    dir=tmp, all=False, id="abc123", out=out, no_redact=False))
            with open(out, encoding="utf-8") as f:
                body = f.read()
        self.assertIn("<title>~/secret/plan.md 정리</title>", body)


class SessionMeta(unittest.TestCase):
    def test_turn_count_matches_opens_turn_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, title="첫 지시", extra=[
                cmd_prompt("/clear"),                      # 커맨드는 턴을 열지 않는다
                "<system-reminder>주입</system-reminder>",  # 시스템 주입도 아니다
                "[Request interrupted by user]",           # 중단 메시지도 아니다
                "둘째 지시",
            ])
        self.assertEqual(s["turns"], 2)

    def test_pr_number_from_pr_link_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp, pr=36441)
        self.assertEqual(s["pr"], 36441)

    def test_no_pr_link_means_no_badge(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = one(tmp)
        self.assertFalse(s["pr"])


class ScanCache(unittest.TestCase):
    def sample(self, tmp):
        write_jsonl(tmp, "s1", "/opt/x", cmd_prompt("/wayfinder", "지도"),
                    "2026-08-01T00:00:00Z", extra=["둘째 지시"], ai_titles=["요약 제목"],
                    pr=42)
        return ccs.scan_sessions([tmp])

    def test_cold_and_warm_scans_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            cold = self.sample(tmp)
            warm = ccs.scan_sessions([tmp])
        self.assertEqual(cold, warm)

    def test_result_identical_without_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            warm = self.sample(tmp)
            os.remove(ccs.cache_path())
            self.assertEqual(ccs.scan_sessions([tmp]), warm)

    def test_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self.sample(tmp)
            with open(ccs.cache_path(), "w") as f:
                f.write("{ 깨진 json")
            self.assertEqual(ccs.scan_sessions([tmp]), good)

    def test_version_mismatch_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self.sample(tmp)
            with open(ccs.cache_path(), "w") as f:
                json.dump({"v": ccs.CACHE_VERSION + 99, "sessions": {}}, f)
            self.assertEqual(ccs.scan_sessions([tmp]), good)

    def test_appended_session_is_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.sample(tmp)
            path = os.path.join(tmp, "s1.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps(
                    {"type": "ai-title", "aiTitle": "새 제목", "sessionId": "s1"}) + "\n")
            os.utime(path, (0, 0))  # mtime 이 바뀌면 캐시가 무효다
            self.assertEqual(ccs.scan_sessions([tmp])[0]["title"], "새 제목")


class IndexHtml(unittest.TestCase):
    def test_two_groups_render_collapsible_path_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", "/opt/alpha", "alpha 작업", "2026-08-01T00:00:00Z")
            write_jsonl(d, "s2", "/opt/beta", "beta 작업", "2026-08-02T00:00:00Z")
            html_out = render_index([d])
        self.assertIn("/opt/alpha", html_out)
        self.assertIn("/opt/beta", html_out)
        self.assertIn('<details class="grp"', html_out)
        self.assertNotIn('<details class="grp" open', html_out)

    def test_worktree_session_shows_badge_in_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(os.path.join(tmp, "repo"))
            wt = git_worktree(os.path.join(tmp, "wt1"), repo, "wt1")
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", repo, "메인 작업", "2026-08-01T00:00:00Z")
            write_jsonl(d, "s2", wt, "워크트리 작업", "2026-08-02T00:00:00Z")
            html_out = render_index([d])
        self.assertIn("워크트리 · wt1", html_out)
        self.assertNotIn('<details class="grp"', html_out)  # 한 그룹 → 플랫

    def test_single_group_stays_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", "/opt/alpha", "첫 작업", "2026-08-01T00:00:00Z")
            write_jsonl(d, "s2", "/opt/alpha", "둘째 작업", "2026-08-02T00:00:00Z")
            html_out = render_index([d])
        self.assertIn("첫 작업", html_out)
        self.assertNotIn('<details class="grp"', html_out)
        self.assertNotIn("/opt/alpha", html_out)

    def test_command_badge_renders_apart_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", "/opt/alpha", cmd_prompt("/x:wayfinder", "지도 그리기"),
                        "2026-08-01T00:00:00Z")
            html_out = render_index([d])
        self.assertIn('<div class="t"><span class="cmd">wayfinder</span>지도 그리기</div>',
                      html_out)

    def test_search_index_covers_title_badge_and_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", "/opt/alpha", cmd_prompt("/x:wayfinder", "무시"),
                        "2026-08-01T00:00:00Z", ai_titles=["헤더 깜빡임 수정"], pr=42)
            row = render_index([d]).split('data-q="')[1].split('"')[0]
        for token in ("깜빡임", "wayfinder", "#42", "alpha"):
            self.assertIn(token, row)

    def test_meta_shows_turns_and_pr_instead_of_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", "/opt/alpha", "첫 지시", "2026-08-01T00:00:00Z",
                        extra=["둘째 지시"], pr=42)
            html_out = render_index([d])
        self.assertIn("<span>2턴</span>", html_out)
        self.assertIn('<span class="pr">#42</span>', html_out)
        self.assertNotIn(" KB</span>", html_out)

    def test_project_shown_only_when_it_distinguishes_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            repo = git_repo(os.path.join(tmp, "alpha"))
            wt = git_worktree(os.path.join(tmp, "beta"), repo, "beta")
            write_jsonl(d, "s1", repo, "첫 작업", "2026-08-01T00:00:00Z")
            same = render_index([d])           # 프로젝트 1종 → 표기 없음
            write_jsonl(d, "s2", wt, "둘째 작업", "2026-08-02T00:00:00Z")
            mixed = render_index([d])          # 같은 repo 그룹, 프로젝트 2종 → 표기
        self.assertNotIn("<span>alpha</span>", same)
        self.assertIn("<span>alpha</span>", mixed)
        self.assertIn("<span>beta</span>", mixed)

    def test_older_session_shows_absolute_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            write_jsonl(d, "s1", "/opt/alpha", "옛 작업", "2020-03-09T00:00:00Z")
            html_out = render_index([d])
        self.assertIn("<span>2020-03-09</span>", html_out)


class TurnCount(unittest.TestCase):
    def test_list_and_detail_agree_on_turn_count(self):
        events = [
            user_text_event("첫 지시"),
            user_text_event(cmd_prompt("/clear")),
            user_text_event("<system-reminder>주입</system-reminder>"),
            user_text_event("This session is being continued from a previous one"),
            user_text_event("둘째 지시"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            with open(path, "w") as f:
                f.write("".join(json.dumps(e) + "\n" for e in events))
            detail = ccs.parse_session(path)
            self.assertEqual(ccs.count_turns(path),
                             max(e["turn"] for e in detail["events"]))

ASK_INPUT = {
    "questions": [
        {
            "question": "색은?", "header": "색", "multiSelect": False,
            "options": [
                {"label": "파랑", "description": "d"},
                {"label": "빨강", "description": "d"},
            ],
        }
    ]
}


def ask_use_event(tid="t1"):
    return {
        "type": "assistant", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "AskUserQuestion",
             "input": ASK_INPUT},
        ]},
    }


def ask_result_event(tid="t1", answers=None):
    e = {
        "type": "user", "timestamp": "2026-08-01T00:00:01Z", "cwd": "/opt/x",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": "Your questions have been answered: ..."},
        ]},
    }
    if answers is not None:
        e["toolUseResult"] = {"questions": ASK_INPUT["questions"], "answers": answers}
    return e


def parse_events(tmp, events):
    path = os.path.join(tmp, "s.jsonl")
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return ccs.parse_session(path)


class ParseSessionAsk(unittest.TestCase):
    def test_answered_question_becomes_ask_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(
                tmp, [ask_use_event(), ask_result_event(answers={"색은?": "파랑"})]
            )
        parts = [p for ev in data["events"] for p in ev["parts"]]
        self.assertEqual(
            [p for p in parts if p["type"] == "ask"],
            [{"type": "ask",
              "items": [{"question": "색은?", "header": "색", "answer": "파랑"}]}],
        )
        self.assertEqual(
            [p["type"] for p in parts if p["type"] in ("tool_use", "tool_result")],
            [],
        )

    def test_unanswered_question_keeps_tool_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(
                tmp, [ask_use_event(), ask_result_event(answers=None)]
            )
        parts = [p for ev in data["events"] for p in ev["parts"]]
        self.assertEqual([p for p in parts if p["type"] == "ask"], [])
        self.assertEqual(
            [p["type"] for p in parts if p["type"] in ("tool_use", "tool_result")],
            ["tool_use", "tool_result"],
        )

PATCH = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
          "lines": ["-a", "+b"]}]


def edit_use_event(tid="e1", file_path="/opt/x/a.py"):
    return {
        "type": "assistant", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Edit",
             "input": {"file_path": file_path, "old_string": "a", "new_string": "b"}},
        ]},
    }


def edit_result_event(tid="e1", file_path="/opt/x/a.py"):
    return {
        "type": "user", "timestamp": "2026-08-01T00:00:01Z", "cwd": "/opt/x",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]},
        "toolUseResult": {"filePath": file_path, "oldString": "a", "newString": "b",
                          "originalFile": "a\n", "replaceAll": False,
                          "structuredPatch": PATCH, "userModified": False},
    }


class ParseSessionFileDiff(unittest.TestCase):
    def test_edit_result_becomes_file_diff_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, [edit_use_event(), edit_result_event()])
        parts = [p for ev in data["events"] for p in ev["parts"]]
        self.assertEqual(
            [p for p in parts if p["type"] == "file_diff"],
            [{"type": "file_diff", "tool": "Edit", "file": "/opt/x/a.py",
              "patch": PATCH, "create": False, "userModified": False}],
        )
        self.assertEqual([p for p in parts if p["type"] == "tool_result"], [])

    def test_write_create_becomes_full_add_diff(self):
        use = {
            "type": "assistant", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "w1", "name": "Write",
                 "input": {"file_path": "/opt/x/new.py", "content": "x = 1\ny = 2"}}]},
        }
        res = {
            "type": "user", "timestamp": "2026-08-01T00:00:01Z", "cwd": "/opt/x",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "w1", "content": "ok"}]},
            "toolUseResult": {"type": "create", "filePath": "/opt/x/new.py",
                              "content": "x = 1\ny = 2", "originalFile": "",
                              "structuredPatch": [], "userModified": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, [use, res])
        parts = [p for ev in data["events"] for p in ev["parts"]]
        fd = [p for p in parts if p["type"] == "file_diff"]
        self.assertEqual(
            fd,
            [{"type": "file_diff", "tool": "Write", "file": "/opt/x/new.py",
              "patch": [], "create": True, "content": "x = 1\ny = 2",
              "userModified": False}],
        )

    def test_failed_edit_keeps_error_tool_result(self):
        res = edit_result_event()
        res["toolUseResult"] = "Error: File has not been read yet."
        res["message"]["content"][0]["is_error"] = True
        res["message"]["content"][0]["content"] = "Error: File has not been read yet."
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, [edit_use_event(), res])
        parts = [p for ev in data["events"] for p in ev["parts"]]
        self.assertEqual([p for p in parts if p["type"] == "file_diff"], [])
        errs = [p for p in parts if p["type"] == "tool_result"]
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0]["error"])


def user_text_event(text, ts="2026-08-01T00:00:00Z"):
    return {
        "type": "user", "timestamp": ts, "cwd": "/opt/x",
        "message": {"role": "user", "content": text},
    }


class ParseSessionTurns(unittest.TestCase):
    def test_turn_increments_on_real_user_utterance_only(self):
        events = [
            user_text_event("첫 요청"),
            edit_use_event(),
            edit_result_event(),  # tool_result user 이벤트는 턴을 안 바꾼다
            user_text_event("둘째 요청", ts="2026-08-01T00:00:02Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual([e["turn"] for e in data["events"]], [1, 1, 1, 2])

    def test_slash_command_alone_does_not_start_turn(self):
        events = [
            user_text_event("첫 요청"),
            user_text_event(
                "<command-name>/clear</command-name><command-args></command-args>",
                ts="2026-08-01T00:00:01Z",
            ),
            user_text_event("둘째 요청", ts="2026-08-01T00:00:02Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual([e["turn"] for e in data["events"]], [1, 1, 2])

    def test_continuation_and_interrupt_do_not_start_turn(self):
        events = [
            user_text_event("첫 요청"),
            user_text_event(
                "This session is being continued from a previous conversation…",
                ts="2026-08-01T00:00:01Z",
            ),
            user_text_event("[Request interrupted by user]", ts="2026-08-01T00:00:02Z"),
            user_text_event("<ide_selection>foo</ide_selection>", ts="2026-08-01T00:00:03Z"),
            user_text_event("둘째 요청", ts="2026-08-01T00:00:04Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual([e["turn"] for e in data["events"]][-1], 2)

    def test_absolute_path_prefix_still_starts_turn(self):
        # 첫 토큰이 통째로 커맨드 이름일 때만 커맨드다 — /var/… 은 경로다
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(
                tmp, [user_text_event("/var/folders/x/handoff.md 읽고 진행")]
            )
        self.assertEqual([e["turn"] for e in data["events"]], [1])

    def test_system_reminder_prefix_still_starts_turn(self):
        events = [
            user_text_event(
                "<system-reminder>안내</system-reminder>\n진짜 지시",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual([e["turn"] for e in data["events"]], [1])

    def test_meta_event_does_not_start_turn(self):
        events = [
            {"type": "user", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
             "isMeta": True, "message": {"role": "user", "content": "메타 안내"}},
            user_text_event("진짜 요청", ts="2026-08-01T00:00:01Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual([e["turn"] for e in data["events"]], [0, 1])


class ParseSessionSubagents(unittest.TestCase):
    def test_subagent_edit_joins_parent_turn_as_sidechain_event(self):
        main = [
            user_text_event("부탁"),
            {"type": "assistant", "timestamp": "2026-08-01T00:00:01Z", "cwd": "/opt/x",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "ag1", "name": "Agent",
                  "input": {"description": "버그 수정", "prompt": "고쳐줘"}}]}},
            {"type": "user", "timestamp": "2026-08-01T00:00:09Z", "cwd": "/opt/x",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "ag1", "content": "done"}]}},
            user_text_event("다음 요청", ts="2026-08-01T00:00:10Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sub = os.path.join(tmp, "s", "subagents")
            os.makedirs(sub)
            with open(os.path.join(sub, "agent-a1.jsonl"), "w") as f:
                for e in [edit_use_event(), edit_result_event()]:
                    e = dict(e, isSidechain=True)
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            with open(os.path.join(sub, "agent-a1.meta.json"), "w") as f:
                json.dump({"agentType": "general-purpose", "description": "버그 수정",
                           "toolUseId": "ag1", "spawnDepth": 1}, f)
            data = parse_events(tmp, main)
        side = [e for e in data["events"] if e["side"]]
        self.assertEqual(len(side), 1)
        self.assertEqual(side[0]["turn"], 1)
        self.assertEqual(side[0]["agent"], "버그 수정")
        self.assertEqual(
            [p["type"] for p in side[0]["parts"]], ["file_diff"]
        )
        # 부모 턴(1) 마지막에 끼어들고, 다음 턴(2) 발화보다 앞선다
        self.assertEqual([e["turn"] for e in data["events"]], [1, 1, 1, 1, 2])
        self.assertEqual(data["counts"]["sidechain"], 1)

    def test_no_subagent_dir_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, [user_text_event("요청")])
        self.assertEqual([e["side"] for e in data["events"]], [False])


def noise_event(i):
    """서브에이전트 파일 부피의 대부분 — diff 와 무관한 탐색·실행 기록."""
    return {
        "type": "user", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "n%d" % i, "content": "가" * 30}]},
    }


def write_subagent(tmp, lines, tool_use_id="ag1"):
    """<tmp>/s/subagents/agent-a1.jsonl 을 쓰고 세션 경로를 돌려준다."""
    sub = os.path.join(tmp, "s", "subagents")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "agent-a1.jsonl"), "w") as f:
        for e in lines:
            f.write(json.dumps(dict(e, isSidechain=True), ensure_ascii=False) + "\n")
    with open(os.path.join(sub, "agent-a1.meta.json"), "w") as f:
        json.dump({"agentType": "general-purpose", "description": "작업",
                   "toolUseId": tool_use_id, "spawnDepth": 1}, f)
    return os.path.join(tmp, "s.jsonl")


class SubagentParseCost(unittest.TestCase):
    """서브에이전트 파일에서 쓰는 건 Edit/Write diff 뿐인데 전 줄을 파싱하면
    세션 열기가 파일 부피에 끌려간다 — 실측 최악 세션은 이 비용이 parse_session
    의 96%(450ms/470ms)였다."""

    # 무관한 줄을 파싱하면 비용이 줄 수에 끌려간다 — 판별이 서게 짧은 줄을 많이 둔다.
    # 실측(이 형태 10.7MB): 전 줄 파싱 84ms vs 걸러낸 뒤 26ms. 임계는 그 사이.
    NOISE = 50000
    LIMIT = 0.05

    def test_bulk_of_unrelated_lines_does_not_dominate_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [noise_event(i) for i in range(self.NOISE)]
            mid = self.NOISE // 2
            lines[mid:mid] = [edit_use_event(), edit_result_event()]
            path = write_subagent(tmp, lines)
            with open(path, "w") as f:
                f.write(json.dumps(user_text_event("부탁"), ensure_ascii=False) + "\n")
            start = time.perf_counter()
            data = ccs.parse_session(path)
            elapsed = time.perf_counter() - start
        side = [e for e in data["events"] if e["side"]]
        self.assertEqual([p["type"] for p in side[0]["parts"]], ["file_diff"])
        self.assertLess(
            elapsed, self.LIMIT,
            "무관한 줄 %d개에 %.0fms — 부피에 끌려간다" % (self.NOISE, elapsed * 1000))


SKILL_BODY = "Base directory for this skill: /x/tdd\n\n# Test-Driven Development\n..."


class ParseSessionSkill(unittest.TestCase):
    def test_skill_prompt_becomes_named_collapsible_part(self):
        events = [
            {"type": "assistant", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "sk1", "name": "Skill",
                  "input": {"skill": "tdd", "args": "x"}}]}},
            {"type": "user", "timestamp": "2026-08-01T00:00:01Z", "cwd": "/opt/x",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "sk1",
                  "content": "Launching skill: tdd"}]}},
            {"type": "user", "timestamp": "2026-08-01T00:00:02Z", "cwd": "/opt/x",
             "isMeta": True, "sourceToolUseID": "sk1",
             "message": {"role": "user", "content": SKILL_BODY}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        parts = [p for ev in data["events"] for p in ev["parts"]]
        self.assertEqual(
            [p for p in parts if p["type"] == "skill"],
            [{"type": "skill", "name": "tdd", "text": SKILL_BODY}],
        )
        self.assertEqual([p for p in parts if p["type"] == "text"], [])

    def test_slash_command_skill_prompt_becomes_skill_part(self):
        body = "Base directory for this skill: /Users/u/.claude/skills/wayfinder\n\n# Wayfinder\n..."
        events = [
            {"type": "user", "timestamp": "2026-08-01T00:00:00Z", "cwd": "/opt/x",
             "isMeta": True,
             "message": {"role": "user", "content": body}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        parts = [p for ev in data["events"] for p in ev["parts"]]
        self.assertEqual(
            [(p["type"], p.get("name")) for p in parts], [("skill", "wayfinder")]
        )


def bash_use_event(cmd, tid="h1", ts="2026-08-01T00:00:00Z"):
    return {
        "type": "assistant", "timestamp": ts, "cwd": "/opt/x",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Bash",
             "input": {"command": cmd}}]},
    }


def bash_result_event(stdout, tid="h1", ts="2026-08-01T00:00:01Z"):
    return {
        "type": "user", "timestamp": ts, "cwd": "/opt/x",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": stdout}]},
        "toolUseResult": {"stdout": stdout, "stderr": "", "interrupted": False},
    }


LIST_STDOUT = """\
user:1785811891781-3  packages/services/x/useToggle.ts [user]
  hunk: 1
  body: 더 좋은 이름이 있을까? toggle은 UI의 관점의 이름 같아.
  author: user

user:1785811937214-4  packages/services/x/test/useToggle.test.tsx [user]
  hunk: 2
  body: 테스트 케이스는 유비쿼터스 언어로 작성해줘.
  author: user
"""


class HunkReviewThreads(unittest.TestCase):
    def test_comment_list_text_stdout_creates_review_threads(self):
        events = [
            bash_use_event("hunk session comment list --repo . --type user 2>&1"),
            bash_result_event(LIST_STDOUT),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        threads = data["reviews"]
        self.assertEqual(
            [(t["file"], [c["body"] for c in t["comments"]]) for t in threads],
            [("packages/services/x/useToggle.ts",
              ["더 좋은 이름이 있을까? toggle은 UI의 관점의 이름 같아."]),
             ("packages/services/x/test/useToggle.test.tsx",
              ["테스트 케이스는 유비쿼터스 언어로 작성해줘."])],
        )
        self.assertEqual(threads[0]["comments"][0]["id"], "user:1785811891781-3")
        self.assertEqual(threads[0]["comments"][0]["hunk"], 1)

    def test_comment_list_json_stdout_captures_range(self):
        stdout = json.dumps({
            "comments": [
                {"noteId": "user:1785116069274", "source": "user",
                 "filePath": "packages/x/AGENTS.md", "hunkIndex": 0,
                 "newRange": [6, 6], "body": "왜 이런 규칙?", "author": "user",
                 "createdAt": "2026-07-27T01:34:29.274Z", "editable": True},
                {"noteId": "mcp:abc:0", "source": "agent",
                 "filePath": "packages/x/AGENTS.md", "hunkIndex": 0,
                 "newRange": [9, 9], "body": "설명", "author": "agent"},
            ]
        }, ensure_ascii=False)
        events = [
            bash_use_event("hunk session comment list --repo . --json"),
            bash_result_event(stdout),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        threads = data["reviews"]
        self.assertEqual(len(threads), 1)
        c = threads[0]["comments"][0]
        self.assertEqual(
            (c["id"], c["hunk"], c["range"], c["created"]),
            ("user:1785116069274", 0, [6, 6], "2026-07-27T01:34:29.274Z"),
        )

    APPLY_CMD = (
        "hunk session reload --repo . -- diff && printf '%s\\n' "
        "'{\"comments\":[\n"
        "{\"filePath\":\"packages/services/x/useToggle.ts\",\"newLine\":16,"
        "\"author\":\"agent\",\"summary\":\"도메인 언어로 리네임\","
        "\"rationale\":\"유즈케이스 문장 기준.\"}\n"
        "]}' | hunk session comment apply --repo . --stdin"
    )
    APPLY_STDOUT = (
        "Reloaded repo /opt/x with working tree (3 files). Selected: a.md hunk 1.\n"
        "Applied 1 live comments to repo /opt/x:\n"
        "  - mcp:6da54d8c-0c5f:0 on packages/services/x/useToggle.ts:16 (new) hunk 1\n"
    )

    def test_apply_reply_attaches_to_same_file_thread(self):
        events = [
            bash_use_event("hunk session comment list --repo . --type user"),
            bash_result_event(LIST_STDOUT),
            bash_use_event(self.APPLY_CMD, tid="h2", ts="2026-08-01T00:00:02Z"),
            bash_result_event(self.APPLY_STDOUT, tid="h2", ts="2026-08-01T00:00:03Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        threads = data["reviews"]
        self.assertEqual(len(threads), 2)
        self.assertEqual(
            threads[0]["replies"],
            [{"file": "packages/services/x/useToggle.ts", "line": 16,
              "status": "new", "summary": "도메인 언어로 리네임",
              "rationale": "유즈케이스 문장 기준."}],
        )
        self.assertEqual(threads[1]["replies"], [])

    def test_unmatched_apply_becomes_agent_only_thread(self):
        cmd = self.APPLY_CMD.replace(
            "packages/services/x/useToggle.ts", "packages/services/x/useConsent.ts"
        )
        stdout = self.APPLY_STDOUT.replace(
            "packages/services/x/useToggle.ts", "packages/services/x/useConsent.ts"
        )
        events = [
            bash_use_event("hunk session comment list --repo . --type user"),
            bash_result_event(LIST_STDOUT),
            bash_use_event(cmd, tid="h2", ts="2026-08-01T00:00:02Z"),
            bash_result_event(stdout, tid="h2", ts="2026-08-01T00:00:03Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        threads = data["reviews"]
        # 리네임 등으로 파일이 안 맞으면 응답만 담긴 단독 스레드로 디그레이드
        self.assertEqual(
            [(t["file"], len(t["comments"]), len(t["replies"])) for t in threads],
            [("packages/services/x/useToggle.ts", 1, 0),
             ("packages/services/x/test/useToggle.test.tsx", 1, 0),
             ("packages/services/x/useConsent.ts", 0, 1)],
        )

    def test_fix_diff_between_list_and_apply_attaches(self):
        f = "packages/services/x/useToggle.ts"
        events = [
            bash_use_event("hunk session comment list --repo . --type user"),
            bash_result_event(LIST_STDOUT),
            edit_use_event(file_path=f),
            edit_result_event(file_path=f),
            bash_use_event(self.APPLY_CMD, tid="h2", ts="2026-08-01T00:00:04Z"),
            bash_result_event(self.APPLY_STDOUT, tid="h2", ts="2026-08-01T00:00:05Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        th = data["reviews"][0]
        self.assertEqual(th["fixes"], [[3, 0]])
        part = data["events"][3]["parts"][0]
        self.assertEqual((part["type"], part["file"]), ("file_diff", f))

    def test_origin_is_last_diff_before_comment(self):
        f = "packages/services/x/useToggle.ts"
        events = [
            edit_use_event(file_path=f),
            edit_result_event(file_path=f),
            bash_use_event("hunk session comment list --repo . --type user",
                           ts="2026-08-01T00:00:02Z"),
            bash_result_event(LIST_STDOUT, ts="2026-08-01T00:00:03Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        th = data["reviews"][0]
        self.assertEqual(th["origin"], [1, 0])
        self.assertIsNone(data["reviews"][1]["origin"])

    def test_duplicate_note_ids_do_not_rethread(self):
        events = [
            bash_use_event("hunk session comment list --repo . --type user"),
            bash_result_event(LIST_STDOUT),
            bash_use_event("hunk session comment list --repo . --type user",
                           tid="h2", ts="2026-08-01T00:00:02Z"),
            bash_result_event(LIST_STDOUT, tid="h2", ts="2026-08-01T00:00:03Z"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual(len(data["reviews"]), 2)

    def test_apply_shell_quote_escape_restored(self):
        cmd = (
            "printf '%s\\n' '{\"comments\":["
            "{\"filePath\":\"a.ts\",\"newLine\":1,\"author\":\"agent\","
            "\"summary\":\"don'\\''t 처리\",\"rationale\":\"r\"}"
            "]}' | hunk session comment apply --repo . --stdin"
        )
        events = [
            bash_use_event(cmd),
            bash_result_event(
                "Applied 1 live comments to repo /opt/x:\n"
                "  - mcp:ab:0 on a.ts:1 (new) hunk 1\n"
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        self.assertEqual(data["reviews"][0]["replies"][0]["summary"], "don't 처리")

    def test_absolute_diff_path_matches_relative_comment_path(self):
        # Edit diff는 절대경로, hunk 코멘트는 repo 상대경로로 남는다 (01 실측)
        f = "/repo/wt/packages/services/x/useToggle.ts"
        events = [
            edit_use_event(file_path=f),
            edit_result_event(file_path=f),
            bash_use_event("hunk session comment list --repo . --type user",
                           ts="2026-08-01T00:00:02Z"),
            bash_result_event(LIST_STDOUT, ts="2026-08-01T00:00:03Z"),
            edit_use_event(tid="e2", file_path=f),
            edit_result_event(tid="e2", file_path=f),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = parse_events(tmp, events)
        th = data["reviews"][0]
        self.assertEqual(th["origin"], [1, 0])
        self.assertEqual(th["fixes"], [[5, 0]])


@contextlib.contextmanager
def serving(dirs):
    """진짜 서버를 띄운다 — 응답 헤더·바디까지가 사용자가 보는 경계다."""
    ccs.Handler.dirs = dirs
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ccs.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, dict(r.headers), r.read().decode("utf-8")


def timed_get(base, path):
    start = time.perf_counter()
    out = get(base, path)
    return out, time.perf_counter() - start


def bulky_session(tmp, sid="s", noise=SubagentParseCost.NOISE):
    """서브에이전트가 큰 세션 하나 — 다시 읽으면 티가 난다."""
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj, exist_ok=True)
    lines = [noise_event(i) for i in range(noise)]
    lines[1:1] = [edit_use_event(), edit_result_event()]
    sub = os.path.join(proj, sid, "subagents")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "agent-a1.jsonl"), "w") as f:
        for e in lines:
            f.write(json.dumps(dict(e, isSidechain=True), ensure_ascii=False) + "\n")
    with open(os.path.join(sub, "agent-a1.meta.json"), "w") as f:
        json.dump({"description": "작업", "toolUseId": "ag1"}, f)
    path = os.path.join(proj, sid + ".jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(user_text_event("부탁"), ensure_ascii=False) + "\n")
    return proj, path


class SessionResponseCache(unittest.TestCase):
    """세션을 다시 열 때마다 파일을 통째로 다시 읽으면, 목록↔상세를 오가는
    평범한 사용이 매번 최악 비용을 낸다. 파일이 그대로면 다시 읽지 않는다."""

    def test_revisit_of_same_session_is_much_cheaper(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = bulky_session(tmp)
            with serving([proj]) as base:
                (first, _, _), t1 = timed_get(base, "/api/session/s")
                (again, _, body), t2 = timed_get(base, "/api/session/s")
        self.assertEqual((first, again), (200, 200))
        self.assertEqual(len(json.loads(body)["events"]), 2)
        self.assertLess(t2, t1 / 5, "재방문이 %.0fms → %.0fms 로만 줄었다"
                        % (t1 * 1000, t2 * 1000))

    def test_appended_session_serves_new_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, path = bulky_session(tmp, noise=10)
            with serving([proj]) as base:
                get(base, "/api/session/s")
                with open(path, "a") as f:
                    f.write(json.dumps(
                        user_text_event("추가 지시", ts="2026-08-01T00:01:00Z"),
                        ensure_ascii=False) + "\n")
                _, _, body = get(base, "/api/session/s")
        texts = [p["text"] for e in json.loads(body)["events"] for p in e["parts"]
                 if p["type"] == "text"]
        self.assertEqual(texts, ["부탁", "추가 지시"])


EMBED_RE = re.compile(
    r'<script id="ccs-data" type="application/json">(.*?)</script>', re.S)


class SessionPagePayload(unittest.TestCase):
    """상세 페이지가 빈 껍데기로 오고 데이터를 다시 받아오면, 세션을 열 때마다
    왕복 한 번과 '불러오는 중…' 구간이 생긴다. 한 응답에 실어 보낸다."""

    def test_page_carries_its_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = bulky_session(tmp, noise=10)
            with serving([proj]) as base:
                _, _, html = get(base, "/s/s")
        m = EMBED_RE.search(html)
        self.assertIsNotNone(m, "상세 페이지가 데이터를 함께 싣지 않는다")
        self.assertEqual(len(json.loads(m.group(1))["events"]), 2)

    def test_markup_in_conversation_cannot_break_out_of_the_data_block(self):
        hostile = "</script><script>window.__pwned=1</script>"
        with tempfile.TemporaryDirectory() as tmp:
            proj, path = bulky_session(tmp, noise=10)
            with open(path, "a") as f:
                f.write(json.dumps(
                    user_text_event(hostile, ts="2026-08-01T00:01:00Z"),
                    ensure_ascii=False) + "\n")
            with serving([proj]) as base:
                _, _, html = get(base, "/s/s")
        m = EMBED_RE.search(html)
        self.assertIsNotNone(m)
        texts = [p["text"] for e in json.loads(m.group(1))["events"]
                 for p in e["parts"] if p["type"] == "text"]
        # 블록이 조기 종료됐다면 위 json.loads 가 이미 깨졌을 것이다.
        self.assertIn(hostile, texts)                       # 내용은 그대로 살아 있고
        self.assertNotIn("<script>window.__pwned", html)    # 태그로는 새지 않는다


class ServerConnection(unittest.TestCase):
    """목록↔상세를 오가는 사용은 요청이 잦다 — 요청마다 연결을 새로 맺게 두지 않는다."""

    def test_one_connection_serves_several_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj, _ = bulky_session(tmp, noise=10)
            with serving([proj]) as base:
                host, _, port = urllib.parse.urlsplit(base).netloc.partition(":")
                conn = http.client.HTTPConnection(host, int(port))
                conn.auto_open = 0      # 끊기면 조용히 새로 잇지 말고 드러나게
                conn.connect()
                try:
                    codes = []
                    for path in ("/", "/s/s", "/"):
                        conn.request("GET", path)
                        r = conn.getresponse()
                        r.read()
                        codes.append(r.status)
                finally:
                    conn.close()
        self.assertEqual(codes, [200, 200, 200])


if __name__ == "__main__":
    unittest.main()
