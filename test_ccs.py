"""ccs 테스트. 실행: python3 -m unittest test_ccs -v"""

import contextlib
import importlib.util
import json
import os
import tempfile
import unittest

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


def write_jsonl(dirpath, sid, cwd, title, ts):
    os.makedirs(dirpath, exist_ok=True)
    line = {
        "type": "user", "timestamp": ts, "cwd": cwd,
        "message": {"role": "user", "content": title},
    }
    with open(os.path.join(dirpath, sid + ".jsonl"), "w") as f:
        f.write(json.dumps(line) + "\n")


def render_index(dirs):
    h = ccs.Handler.__new__(ccs.Handler)
    h.dirs = dirs
    return ccs.Handler.index(h)


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


if __name__ == "__main__":
    unittest.main()
