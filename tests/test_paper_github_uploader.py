import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from src.paper_github_uploader import upload_daily_artifacts

IST = ZoneInfo("Asia/Kolkata")


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class FakeGitHub:
    def __init__(self, *, private=True):
        self.private = private
        self.calls = []
        self.blobs = {}
        self.tree_entries = []
        self.committed = False

    def open(self, request, timeout):
        method = request.get_method()
        parsed = urlsplit(request.full_url)
        path = parsed.path
        payload = json.loads(request.data) if request.data else None
        self.calls.append((method, path, payload, timeout))

        if method == "GET" and path == "/repos/acme/research":
            return FakeResponse({"private": self.private, "default_branch": "main"})
        if method == "GET" and path == "/repos/acme/research/git/ref/heads/main":
            return FakeResponse({"object": {"sha": "parent-commit"}})
        if method == "GET" and path == "/repos/acme/research/git/commits/parent-commit":
            return FakeResponse({"tree": {"sha": "base-tree"}})
        if method == "GET" and path.startswith("/repos/acme/research/contents/"):
            return FakeResponse([])
        if method == "POST" and path == "/repos/acme/research/git/blobs":
            raw = base64.b64decode(payload["content"])
            sha = _blob_sha(raw)
            self.blobs[sha] = raw
            return FakeResponse({"sha": sha})
        if method == "POST" and path == "/repos/acme/research/git/trees":
            self.tree_entries = payload["tree"]
            return FakeResponse({"sha": "new-tree"})
        if method == "POST" and path == "/repos/acme/research/git/commits":
            assert payload["tree"] == "new-tree"
            assert payload["parents"] == ["parent-commit"]
            self.committed = True
            return FakeResponse({"sha": "new-commit"})
        if method == "PATCH" and path == "/repos/acme/research/git/refs/heads/main":
            assert payload == {"sha": "new-commit", "force": False}
            return FakeResponse({"object": {"sha": "new-commit"}})
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")


def test_upload_creates_one_private_daily_commit_and_dated_folder(tmp_path):
    report = tmp_path / "reports" / "paper_daily_2026-11-03.md"
    report.parent.mkdir()
    report.write_text("# daily report\nresearch-only\n", encoding="utf-8")
    artifacts = [{
        "label": "Daily Markdown report",
        "path": str(report),
        "relative_path": "reports/paper_daily_2026-11-03.md",
        "category": "reports",
        "required": True,
        "exists": True,
        "upload": True,
    }]
    fake = FakeGitHub()
    result = upload_daily_artifacts(
        root=tmp_path,
        artifacts=artifacts,
        report_date="2026-11-03",
        now=datetime(2026, 11, 3, 15, 20, tzinfo=IST),
        environ={
            "TRADINGGPT_RESEARCH_REPO": "acme/research",
            "TRADINGGPT_RESEARCH_TOKEN": "github_pat_test_token_value",
        },
        opener=fake.open,
    )
    assert result["ok"] is True
    assert result["remote_folder"] == "daily/2026-11-03"
    assert result["uploaded_files"] == 2  # report plus manifest
    assert fake.committed is True
    paths = {item["path"] for item in fake.tree_entries}
    assert "daily/2026-11-03/reports/paper_daily_2026-11-03.md" in paths
    assert "daily/2026-11-03/manifest.json" in paths
    assert any(method == "PATCH" for method, *_ in fake.calls)


def test_upload_refuses_public_research_repository_before_writing(tmp_path):
    report = tmp_path / "daily.md"
    report.write_text("safe research data", encoding="utf-8")
    fake = FakeGitHub(private=False)
    result = upload_daily_artifacts(
        root=tmp_path,
        artifacts=[{
            "path": str(report), "category": "reports", "exists": True,
            "required": True, "upload": True,
        }],
        report_date="2026-11-03",
        environ={
            "TRADINGGPT_RESEARCH_REPO": "acme/research",
            "TRADINGGPT_RESEARCH_TOKEN": "github_pat_test_token_value",
        },
        opener=fake.open,
    )
    assert result["ok"] is False
    assert "not a private repository" in result["message"]
    assert not any(method in {"POST", "PATCH"} for method, *_ in fake.calls)


def test_upload_without_configuration_is_a_safe_noop(tmp_path):
    result = upload_daily_artifacts(
        root=tmp_path,
        artifacts=[],
        report_date="2026-11-03",
        environ={},
    )
    assert result["ok"] is False
    assert result["configured"] is False
    assert "TRADINGGPT_RESEARCH_REPO" in result["message"]


def test_upload_refuses_files_outside_project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("x,y\n1,2\n", encoding="utf-8")
    result = upload_daily_artifacts(
        root=root,
        artifacts=[{
            "path": str(outside), "category": "candles",
            "exists": True, "required": True, "upload": True,
        }],
        report_date="2026-11-03",
        environ={
            "TRADINGGPT_RESEARCH_REPO": "acme/research",
            "TRADINGGPT_RESEARCH_TOKEN": "github_pat_test_token_value",
        },
    )
    assert result["ok"] is False
    assert "outside the project root" in result["message"]


def test_upload_refuses_token_patterns_in_artifacts_before_network_write(tmp_path):
    report = tmp_path / "paper_daily.json"
    report.write_text('{"access_token":"this-is-a-fake-long-token-value"}', encoding="utf-8")
    fake = FakeGitHub()
    result = upload_daily_artifacts(
        root=tmp_path,
        artifacts=[{
            "path": str(report), "category": "reports",
            "exists": True, "required": True, "upload": True,
        }],
        report_date="2026-11-03",
        environ={
            "TRADINGGPT_RESEARCH_REPO": "acme/research",
            "TRADINGGPT_RESEARCH_TOKEN": "github_pat_test_token_value",
        },
        opener=fake.open,
    )
    assert result["ok"] is False
    assert "Potential credential/token pattern" in result["message"]
    assert fake.calls == []



def test_required_artifact_over_github_api_limit_fails_closed(tmp_path, monkeypatch):
    from src import paper_github_uploader as uploader

    report = tmp_path / "paper_daily.md"
    report.write_text("this content is larger than the configured upload limit", encoding="utf-8")
    monkeypatch.setattr(uploader, "MAX_FILE_BYTES", 8)
    result = uploader.upload_daily_artifacts(
        root=tmp_path,
        artifacts=[{
            "path": str(report), "category": "reports",
            "exists": True, "required": True, "upload": True,
        }],
        report_date="2026-11-03",
        environ={
            "TRADINGGPT_RESEARCH_REPO": "acme/research",
            "TRADINGGPT_RESEARCH_TOKEN": "github_pat_test_token_value",
        },
        opener=FakeGitHub().open,
    )
    assert result["ok"] is False
    assert "Required artifact is too large" in result["message"]
