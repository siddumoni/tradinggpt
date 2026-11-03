"""Upload finalized TradingGPT research artifacts to a verified private GitHub repo.

The uploader uses a fine-grained GitHub token from the process environment and the
Git Database API to create one commit per finalized daily batch. It deliberately
refuses public repositories and never uploads the local SQLite database.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LOGGER = logging.getLogger("paper_github_uploader")
API_ROOT = "https://api.github.com"
MAX_FILE_BYTES = 60 * 1024 * 1024
ALLOWED_CATEGORIES = {"reports", "scans", "diagnostics", "logs", "candles"}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
SECRET_TEXT_PATTERNS = (
    re.compile(
        rb"(?im)(?:kite[_-]?api[_-]?key|api[_-]?key|api[_-]?secret|access[_-]?token)"
        rb"\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./+-]{12,}"
    ),
    re.compile(rb"(?im)authorization\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9._~+/-]{16,}"),
    re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
)
DENIED_NAMES = {
    ".env", ".env.local", ".env.production", "credentials.json",
    "token.json", "access_token", "kite_session.json",
}


class GitHubUploadError(RuntimeError):
    """Safe-to-log GitHub upload error; never includes request headers or tokens."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _report_day(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.astimezone(IST).date().isoformat() if value.tzinfo else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    parsed = date.fromisoformat(str(value))
    return parsed.isoformat()


class GitHubPrivateResearchUploader:
    """Minimal GitHub Git-Database API client that commits a whole daily batch once."""

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        branch: str | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: float = 45.0,
    ):
        if not REPO_RE.fullmatch(repository):
            raise ValueError("TRADINGGPT_RESEARCH_REPO must have owner/repository format.")
        if not token or token.strip() != token:
            raise ValueError("A non-empty TRADINGGPT_RESEARCH_TOKEN is required.")
        if branch is not None and (
            not BRANCH_RE.fullmatch(branch) or ".." in branch.split("/")
        ):
            raise ValueError("TRADINGGPT_RESEARCH_BRANCH contains invalid characters.")
        self.repository = repository
        self.token = token
        self.branch = branch
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _request(self, method: str, endpoint: str, payload: dict | None = None) -> Any:
        url = f"{API_ROOT}{endpoint}"
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TradingGPT-Private-Research-Uploader",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                detail = exc.read(2048).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            message = f"GitHub API {method} failed with HTTP {status}"
            try:
                parsed = json.loads(detail)
                if parsed.get("message"):
                    message += f": {parsed['message']}"
            except (TypeError, ValueError):
                pass
            raise GitHubUploadError(message, status) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubUploadError(
                f"GitHub API {method} request failed: {type(exc).__name__}"
            ) from None

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubUploadError("GitHub API returned a non-JSON response.") from None

    def _get_optional(self, endpoint: str) -> Any | None:
        try:
            return self._request("GET", endpoint)
        except GitHubUploadError as exc:
            if exc.status_code == 404:
                return None
            raise

    def upload(
        self,
        *,
        report_date: str,
        files: list[dict],
        generated_at: datetime,
    ) -> dict:
        day = _report_day(report_date)
        repo_path = f"/repos/{self.repository}"
        repo_info = self._request("GET", repo_path)
        if repo_info.get("private") is not True:
            raise GitHubUploadError(
                f"Refusing upload: {self.repository} is not a private repository. "
                "Research artifacts are never written to public repositories."
            )

        branch = self.branch or str(repo_info.get("default_branch") or "main")
        if not BRANCH_RE.fullmatch(branch) or ".." in branch.split("/"):
            raise GitHubUploadError("The configured research repository branch is invalid.")
        encoded_branch = urllib.parse.quote(branch, safe="/")
        ref = self._request(
            "GET", f"{repo_path}/git/ref/heads/{encoded_branch}"
        )
        parent_sha = str((ref.get("object") or {}).get("sha") or "")
        if not parent_sha:
            raise GitHubUploadError(
                f"Could not resolve branch '{branch}' in private repository {self.repository}."
            )
        parent_commit = self._request(
            "GET", f"{repo_path}/git/commits/{parent_sha}"
        )
        base_tree_sha = str((parent_commit.get("tree") or {}).get("sha") or "")
        if not base_tree_sha:
            raise GitHubUploadError("Could not resolve the current research repository tree.")

        remote_root = f"daily/{day}"
        categories = sorted({str(item["category"]) for item in files})
        remote_items: dict[str, dict] = {}

        # List the dated root as well as category folders. The root listing
        # contains manifest.json, which allows unchanged daily batches to be
        # recognized without producing another Git commit on graceful shutdown.
        root_endpoint = (
            f"{repo_path}/contents/{remote_root}"
            f"?ref={urllib.parse.quote(branch, safe='')}"
        )
        root_entries = self._get_optional(root_endpoint) or []
        if isinstance(root_entries, list):
            for item in root_entries:
                if item.get("type") == "file" and item.get("path"):
                    remote_items[str(item["path"])] = item

        for category in categories:
            if category not in ALLOWED_CATEGORIES:
                raise GitHubUploadError(f"Unsupported upload category: {category}")
            endpoint = (
                f"{repo_path}/contents/{remote_root}/{category}"
                f"?ref={urllib.parse.quote(branch, safe='')}"
            )
            existing = self._get_optional(endpoint) or []
            if isinstance(existing, list):
                for item in existing:
                    if item.get("type") == "file" and item.get("path"):
                        remote_items[str(item["path"])] = item

        tree_entries: list[dict] = []
        manifest_files: list[dict] = []
        unchanged = 0
        uploaded = 0
        for item in files:
            category = str(item["category"])
            name = Path(str(item["path"])).name
            remote_path = f"{remote_root}/{category}/{name}"
            content = item["content"]
            sha256 = hashlib.sha256(content).hexdigest()
            manifest_files.append({
                "path": remote_path,
                "category": category,
                "size_bytes": len(content),
                "sha256": sha256,
                "required": bool(item.get("required")),
            })
            blob_sha = _git_blob_sha(content)
            existing = remote_items.get(remote_path)
            if existing and existing.get("sha") == blob_sha:
                unchanged += 1
                continue

            blob = self._request(
                "POST",
                f"{repo_path}/git/blobs",
                {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
            )
            new_blob_sha = str(blob.get("sha") or "")
            if not new_blob_sha:
                raise GitHubUploadError(f"GitHub did not return a blob SHA for {name}.")
            tree_entries.append({
                "path": remote_path,
                "mode": "100644",
                "type": "blob",
                "sha": new_blob_sha,
            })
            uploaded += 1

        manifest = {
            "project": "TradingGPT",
            "report_date": day,
            "repository": self.repository,
            "research_only": True,
            "orders_sent_to_broker": False,
            "uploaded_file_count": len(files),
            "files": manifest_files,
            "excluded_by_design": [
                ".env and credential/token files",
                "SQLite paper-account database",
                "files outside the project root",
            ],
        }
        manifest_bytes = json.dumps(
            manifest, indent=2, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        manifest_path = f"{remote_root}/manifest.json"
        manifest_sha = _git_blob_sha(manifest_bytes)
        existing_manifest = remote_items.get(manifest_path)
        if existing_manifest and existing_manifest.get("sha") == manifest_sha:
            unchanged += 1
        else:
            blob = self._request(
                "POST",
                f"{repo_path}/git/blobs",
                {
                    "content": base64.b64encode(manifest_bytes).decode("ascii"),
                    "encoding": "base64",
                },
            )
            manifest_blob_sha = str(blob.get("sha") or "")
            if not manifest_blob_sha:
                raise GitHubUploadError("GitHub did not return a manifest blob SHA.")
            tree_entries.append({
                "path": manifest_path,
                "mode": "100644",
                "type": "blob",
                "sha": manifest_blob_sha,
            })
            uploaded += 1

        if not tree_entries:
            return {
                "ok": True,
                "repository": self.repository,
                "branch": branch,
                "remote_folder": remote_root,
                "uploaded_files": 0,
                "unchanged_files": unchanged,
                "commit_sha": None,
                "message": "All daily artifacts are already up to date.",
            }

        new_tree = self._request(
            "POST",
            f"{repo_path}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        new_tree_sha = str(new_tree.get("sha") or "")
        if not new_tree_sha:
            raise GitHubUploadError("GitHub did not return the updated tree SHA.")
        commit = self._request(
            "POST",
            f"{repo_path}/git/commits",
            {
                "message": f"Add TradingGPT paper research artifacts for {day}",
                "tree": new_tree_sha,
                "parents": [parent_sha],
            },
        )
        commit_sha = str(commit.get("sha") or "")
        if not commit_sha:
            raise GitHubUploadError("GitHub did not return the daily upload commit SHA.")
        self._request(
            "PATCH",
            f"{repo_path}/git/refs/heads/{encoded_branch}",
            {"sha": commit_sha, "force": False},
        )
        return {
            "ok": True,
            "repository": self.repository,
            "branch": branch,
            "remote_folder": remote_root,
            "uploaded_files": uploaded,
            "unchanged_files": unchanged,
            "commit_sha": commit_sha,
            "message": "Daily research artifacts uploaded in one commit.",
        }


def _prepare_files(root: Path, artifacts: list[dict]) -> tuple[list[dict], list[str]]:
    root = Path(root).resolve()
    ready: list[dict] = []
    skipped: list[str] = []
    seen_paths: set[str] = set()
    for item in artifacts:
        if not item.get("upload", True) or not item.get("exists", True):
            continue
        path = Path(item["path"])
        if path.is_symlink():
            raise GitHubUploadError(f"Refusing to upload symbolic link: {path.name}")
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            raise GitHubUploadError(
                f"Refusing to upload a file outside the project root: {path}"
            ) from None
        if not resolved.is_file():
            skipped.append(f"{relative.as_posix()}: not a regular file")
            continue
        name_lower = resolved.name.lower()
        if (
            name_lower in DENIED_NAMES
            or name_lower.startswith(".env.")
            or "credential" in name_lower
            or name_lower.endswith((".sqlite", ".sqlite3", ".db", ".token"))
        ):
            skipped.append(f"{relative.as_posix()}: excluded sensitive/runtime file")
            continue
        category = str(item.get("category") or "")
        if category not in ALLOWED_CATEGORIES:
            raise GitHubUploadError(f"Unsupported artifact category for {relative.as_posix()}.")
        file_size = resolved.stat().st_size
        if file_size > MAX_FILE_BYTES:
            message = (
                f"{relative.as_posix()}: {file_size} bytes exceeds the 60 MiB GitHub API upload limit"
            )
            if item.get("required"):
                raise GitHubUploadError(
                    f"Required artifact is too large for the GitHub API: {relative.as_posix()}. "
                    "Use Git LFS or object storage before enabling automatic uploads."
                )
            skipped.append(message)
            continue
        content = resolved.read_bytes()
        if any(pattern.search(content) for pattern in SECRET_TEXT_PATTERNS):
            raise GitHubUploadError(
                f"Potential credential/token pattern found in {relative.as_posix()}; "
                "the entire upload was cancelled for safety."
            )
        remote_key = f"{category}/{resolved.name}"
        if remote_key in seen_paths:
            raise GitHubUploadError(f"Duplicate destination filename: {remote_key}")
        seen_paths.add(remote_key)
        ready.append({
            "path": str(resolved),
            "relative_path": relative.as_posix(),
            "category": category,
            "required": bool(item.get("required")),
            "content": content,
        })
    return ready, skipped


def upload_daily_artifacts(
    *,
    root: Path,
    artifacts: list[dict],
    report_date: date | datetime | str,
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict:
    """Upload READY checklist artifacts to a private research repo, or fail closed."""
    env = os.environ if environ is None else environ
    repository = (env.get("TRADINGGPT_RESEARCH_REPO") or "").strip()
    token = env.get("TRADINGGPT_RESEARCH_TOKEN") or ""
    branch = (env.get("TRADINGGPT_RESEARCH_BRANCH") or "").strip() or None
    if not repository or not token:
        return {
            "ok": False,
            "configured": False,
            "message": (
                "GitHub research upload is not configured. Set TRADINGGPT_RESEARCH_REPO "
                "and TRADINGGPT_RESEARCH_TOKEN in Windows user environment variables."
            ),
        }

    day = _report_day(report_date)
    generated_at = now or datetime.now(IST)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=IST)
    else:
        generated_at = generated_at.astimezone(IST)

    missing = [
        str(item.get("label") or item.get("path"))
        for item in artifacts
        if item.get("required") and not item.get("exists")
    ]
    if missing:
        return {
            "ok": False,
            "configured": True,
            "message": "Required report artifacts are missing: " + ", ".join(missing[:10]),
        }

    try:
        files, skipped = _prepare_files(Path(root), artifacts)
        if not files:
            return {
                "ok": False,
                "configured": True,
                "message": "No eligible daily artifacts were found for upload.",
                "skipped_files": skipped,
            }
        uploader = GitHubPrivateResearchUploader(
            repository=repository, token=token, branch=branch, opener=opener
        )
        result = uploader.upload(
            report_date=day, files=files, generated_at=generated_at
        )
        result.update({
            "configured": True,
            "skipped_files": skipped,
            "file_count": len(files),
        })
        return result
    except (GitHubUploadError, OSError, ValueError) as exc:
        LOGGER.error("Private GitHub research upload failed: %s", exc)
        return {
            "ok": False,
            "configured": True,
            "repository": repository,
            "message": str(exc),
        }
