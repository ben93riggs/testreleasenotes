#!/usr/bin/env python3
"""
Generate release notes for a GitHub issue using Google Gemini AI.
Triggered by the generate-release-notes GitHub Actions workflow when an issue
is labeled with 'needs-documentation'.

Workflow:
  1. Fetch issue body, comments, and related commits
  2. Determine target version from milestone or pom.xml
  3. Call Gemini to draft an AsciiDoc entry in the project's house style
  4. Insert the entry into the appropriate release notes file(s)
  5. Open a PR on a dedicated branch for human review
  6. Swap labels: remove 'needs-documentation', add 'needs-documentation-confirmed'
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import google.generativeai as genai
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GOOGLE_AI_API_KEY = os.environ["GOOGLE_AI_API_KEY"]
ISSUE_NUMBER    = int(os.environ["ISSUE_NUMBER"])
REPO              = os.environ["GITHUB_REPOSITORY"]   # "owner/repo"

GH_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
GH_API = f"https://api.github.com/repos/{REPO}"

FRAMEWORK_NOTES_DIR = Path(
    "Documentation/asciidoc/framework-release-notes"
    "/src/main/asciidoc/modules/ROOT/pages"
)
ADDIN_NOTES_DIR = Path(
    "Documentation/asciidoc/addin-release-notes"
    "/src/main/asciidoc/modules/ROOT/pages"
)

NEEDS_DOC_LABEL = "needs-documentation"
CONFIRMED_LABEL = "needs-documentation-confirmed"

# ── GitHub API helpers ─────────────────────────────────────────────────────────

def gh_get(url: str, **params) -> "dict | list":
    r = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def gh_post(url: str, data: dict) -> dict:
    r = requests.post(url, headers=GH_HEADERS, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def gh_delete(url: str) -> None:
    r = requests.delete(url, headers=GH_HEADERS, timeout=30)
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


# ── Issue data ─────────────────────────────────────────────────────────────────

def get_issue() -> dict:
    return gh_get(f"{GH_API}/issues/{ISSUE_NUMBER}")


def get_issue_comments() -> list:
    return gh_get(f"{GH_API}/issues/{ISSUE_NUMBER}/comments", per_page=100)


def get_related_commits() -> list[str]:
    """Find commits whose messages reference this issue number."""
    result = subprocess.run(
        ["git", "log", "--oneline", "--all", f"--grep=#{ISSUE_NUMBER}"],
        capture_output=True, text=True,
    )
    summaries = []
    for line in result.stdout.strip().splitlines()[:10]:
        if not line:
            continue
        sha = line.split()[0]
        detail = subprocess.run(
            ["git", "log", "-1", "--format=%B", sha],
            capture_output=True, text=True,
        )
        summaries.append(f"Commit {sha}:\n{detail.stdout.strip()}")
    return summaries


# ── Version determination ──────────────────────────────────────────────────────

def get_pom_version() -> str:
    """Read current version from root pom.xml, stripping -SNAPSHOT."""
    content = Path("pom.xml").read_text(encoding="utf-8")
    match = re.search(r"<version>([^<]+)</version>", content)
    if not match:
        raise ValueError("Could not determine version from pom.xml")
    return match.group(1).replace("-SNAPSHOT", "")


def get_target_version(issue: dict) -> str:
    """Prefer the milestone title; fall back to the current pom version."""
    milestone = issue.get("milestone")
    if milestone:
        title = milestone["title"].strip()
        m = re.match(r"^(\d+\.\d+\.\d+)", title)
        if m:
            return m.group(1)
    return get_pom_version()


def get_base_branch(version: str) -> str:
    """
    Return 'develop' for the current dev version, or the matching release
    branch name when backporting to an older version.
    """
    if version == get_pom_version():
        return "develop"
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{version}"],
        capture_output=True, text=True,
    )
    return version if result.stdout.strip() else "develop"


# ── Release notes file lookup ─────────────────────────────────────────────────

def find_notes_file(notes_dir: Path, version: str) -> "Path | None":
    """Return the first .adoc file whose name starts with the version string."""
    for f in sorted(notes_dir.glob("*.adoc")):
        if f.name.startswith(version):
            return f
    return None


def version_to_anchor_prefix(version: str) -> str:
    """'8.7.0' → '8_7_0'"""
    return version.replace(".", "_")


# ── AsciiDoc modification ─────────────────────────────────────────────────────

# Matches the "no changes" placeholder line used before any entries exist.
_NO_CHANGES_RE = re.compile(
    r"_There are no changes to (?:the Dodeca client|the Excel Add-In)"
    r" in this release\._\n?"
)


def insert_entry(content: str, version: str, section_name: str, entry: str) -> str:
    """
    Insert *entry* (the full bullet text, without the leading '* ') into the
    release notes *content* under *section_name* for the given *version*.

    Handles:
      - Appending to an existing section
      - Creating a brand-new section (TOC entry + section body)
      - Replacing the '_There are no changes_' placeholder
    """
    anchor_prefix   = version_to_anchor_prefix(version)
    section_anchor  = f"{anchor_prefix}_{section_name.replace(' ', '_')}"
    section_header  = f"[#{section_anchor}]"
    toc_entry       = f"* <<#{section_anchor},{section_name}>>"
    full_bullet     = f"* {entry}"

    # ── Case 1: section already exists → append bullet ────────────────────────
    if section_header in content:
        pos  = content.index(section_header)
        rest = content[pos:]

        # Skip past the [#anchor] line and the === heading line
        heading_match = re.search(r"=== .+\n", rest)
        if not heading_match:
            raise ValueError(f"Could not find === heading for [{section_anchor}]")
        body_start = pos + heading_match.end()

        # Find where this section ends: next [# anchor or the closing ifdef comment
        end_match = re.search(
            r"(\n\[#|\n// if using the antora backend, restore)",
            content[body_start:],
        )
        body_end = body_start + end_match.start() if end_match else len(content)

        body     = content[body_start:body_end]
        new_body = body.rstrip() + f"\n{full_bullet}\n\n"
        return content[:body_start] + new_body + content[body_end:]

    # ── Case 2: new section needed ────────────────────────────────────────────

    # 2a. Handle the "no changes" placeholder
    if _NO_CHANGES_RE.search(content):
        content = _NO_CHANGES_RE.sub(
            f"_This release contains the following changes:_\n\n{toc_entry}\n",
            content,
        )
    else:
        # 2b. Append to an existing TOC
        toc_line_re = re.compile(r"^(?:\* )?<<#[^>]+>>[^\n]*$", re.MULTILINE)
        toc_matches = list(toc_line_re.finditer(content))
        if toc_matches:
            ins = toc_matches[-1].end()
            content = content[:ins] + f"\n{toc_entry}" + content[ins:]
        else:
            # 2c. No TOC yet — add one after the intro sentence or before <<<
            intro_re = re.compile(
                r"(The release notes for this version contain[^\n]*\n"
                r"|_This release contains[^\n]*\n)",
            )
            intro_match = intro_re.search(content)
            page_break  = content.find("<<<")

            if intro_match:
                ins     = intro_match.end()
                content = content[:ins] + f"\n{toc_entry}\n" + content[ins:]
            elif page_break >= 0:
                content = content[:page_break] + f"{toc_entry}\n\n" + content[page_break:]
            else:
                # Addin-style — add before closing ifdef
                closing = "// if using the antora backend, restore"
                cpos    = content.find(closing)
                if cpos >= 0:
                    content = (
                        content[:cpos]
                        + f"_This release contains the following changes:_\n\n"
                        + f"{toc_entry}\n\n"
                        + content[cpos:]
                    )

    # 2d. Insert the new section body before the closing ifdef comment
    closing     = "// if using the antora backend, restore"
    closing_pos = content.find(closing)
    new_section = (
        f"\n[#{section_anchor}]\n"
        f"=== {section_name}\n\n"
        f"{full_bullet}\n\n"
    )

    if closing_pos >= 0:
        before = content[:closing_pos].rstrip() + "\n"
        return before + new_section + content[closing_pos:]
    else:
        return content.rstrip() + new_section


# ── Gemini integration ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a technical writer generating release notes for Dodeca, a spreadsheet
management system (OLAP tool) built by Applied OLAP.

Release notes are written in AsciiDoc and follow strict conventions:
- Every entry starts with one of: "Enhancement:", "Fixed Issue:", "New Method:", or "Known Issue:"
- The entry ends with the GitHub issue number: " #<number>"
- Use backticks for code/method/property names and *bold* for UI element names
- Keep descriptions to 1–3 sentences; focus on user-visible impact
- Section names match the product feature area (e.g., "Connections Editor", "Workbook Scripting")

Determine whether the change belongs in:
  - FRAMEWORK notes (server-side, client framework, views, scripting, config, metadata explorer)
  - EXCEL ADD-IN notes (Excel-specific features, EPM Cloud via addin, addin UI)
  - Both (rare — only when a single issue touches both products)
"""


def call_gemini(
    issue:             dict,
    comments:          list,
    commits:           list[str],
    framework_content: str,
    addin_content:     str,
    version:           str,
) -> dict:
    """
    Ask Gemini to generate a structured JSON with the release notes entry.

    Returns a dict like:
    {
      "framework": {"applicable": true,  "section_name": "...", "entry": "..."},
      "addin":     {"applicable": false}
    }
    """
    genai.configure(api_key=GOOGLE_AI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=_SYSTEM_PROMPT,
    )

    labels_str   = ", ".join(lb["name"] for lb in issue.get("labels", [])) or "none"
    comments_str = "\n\n".join(
        f"Comment by @{c['user']['login']}:\n{c['body']}"
        for c in comments if c.get("body")
    ) or "No comments."
    commits_str  = "\n\n".join(commits) or "No related commits found."

    prompt = f"""\
Issue #{ISSUE_NUMBER}: {issue.get('title', '')}
Labels: {labels_str}

Body:
{issue.get('body') or 'No body provided.'}

Comments:
{comments_str}

Related commits:
{commits_str}

Target version: {version}

Current FRAMEWORK release notes file ({version}_dodeca_release.adoc):
```asciidoc
{framework_content or '(file not found)'}
```

Current EXCEL ADD-IN release notes file ({version}.adoc):
```asciidoc
{addin_content or '(file not found)'}
```

Generate a release notes entry for this issue.

Prefer matching an existing section name over inventing a new one.
If a new section is needed, choose a short, title-case feature-area name.

Return ONLY a JSON object — no markdown fences, no explanation:
{{
  "framework": {{
    "applicable": true,
    "section_name": "Section Name",
    "entry": "Enhancement: Description of the change. #{ISSUE_NUMBER}"
  }},
  "addin": {{
    "applicable": false
  }}
}}

If applicable is false, omit section_name and entry for that product."""

    response = model.generate_content(prompt)
    text = response.text.strip()
    # Strip markdown code fences if the model wraps the JSON anyway
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```$", "", text.strip())

    return json.loads(text)


# ── Git / PR operations ────────────────────────────────────────────────────────

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def create_pr(
    modified_files: "list[tuple[Path, str]]",
    version:        str,
    base_branch:    str,
    generated:      dict,
) -> str:
    """Commit changes, push branch, open PR. Returns the PR HTML URL."""
    branch = f"release-notes/{version}/issue-{ISSUE_NUMBER}"

    _git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
    _git("config", "user.name",  "github-actions[bot]")
    _git("checkout", "-b", branch)

    for file_path, new_content in modified_files:
        file_path.write_text(new_content, encoding="utf-8")
        _git("add", str(file_path))

    _git("commit", "-m", f"Release Notes: Add #{ISSUE_NUMBER}")
    _git("push", "origin", branch)

    fw = generated.get("framework", {})
    ad = generated.get("addin",    {})

    body_lines = [
        f"This PR adds auto-generated release notes for issue #{ISSUE_NUMBER}.",
        "",
        "Please review the generated content for accuracy and style before merging.",
        "",
    ]
    if fw.get("applicable"):
        body_lines += [
            f"**Framework ({version})** — _{fw['section_name']}_",
            f"```",
            f"* {fw['entry']}",
            f"```",
            "",
        ]
    if ad.get("applicable"):
        body_lines += [
            f"**Excel Add-In ({version})** — _{ad['section_name']}_",
            f"```",
            f"* {ad['entry']}",
            f"```",
            "",
        ]
    body_lines += [
        "---",
        f"🤖 Generated by the [release notes workflow](https://github.com/{REPO}/actions)",
    ]

    pr = gh_post(f"{GH_API}/pulls", {
        "title": f"Release Notes: Add #{ISSUE_NUMBER}",
        "body":  "\n".join(body_lines),
        "head":  branch,
        "base":  base_branch,
    })
    return pr["html_url"]


# ── Label management ───────────────────────────────────────────────────────────

def update_labels() -> None:
    gh_post(f"{GH_API}/issues/{ISSUE_NUMBER}/labels", {"labels": [CONFIRMED_LABEL]})
    gh_delete(f"{GH_API}/issues/{ISSUE_NUMBER}/labels/{NEEDS_DOC_LABEL}")


def post_comment(body: str) -> None:
    gh_post(f"{GH_API}/issues/{ISSUE_NUMBER}/comments", {"body": body})


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Generating release notes for issue #{ISSUE_NUMBER} in {REPO}")

    issue    = get_issue()
    comments = get_issue_comments()
    commits  = get_related_commits()

    version     = get_target_version(issue)
    base_branch = get_base_branch(version)
    print(f"Version: {version}  |  base branch: {base_branch}")

    fw_file   = find_notes_file(FRAMEWORK_NOTES_DIR, version)
    addin_file = find_notes_file(ADDIN_NOTES_DIR,    version)

    fw_content    = fw_file.read_text(encoding="utf-8")    if fw_file    else ""
    addin_content = addin_file.read_text(encoding="utf-8") if addin_file else ""

    if not fw_content and not addin_content:
        msg = (
            f"⚠️ No release notes files found for version **{version}**. "
            "Please add release notes manually."
        )
        print(msg)
        post_comment(msg)
        update_labels()
        sys.exit(0)

    print("Calling Gemini to generate release notes…")
    generated = call_gemini(issue, comments, commits, fw_content, addin_content, version)
    print("Claude response:\n" + json.dumps(generated, indent=2))

    fw_result   = generated.get("framework", {})
    addin_result = generated.get("addin",    {})

    modified: "list[tuple[Path, str]]" = []

    if fw_result.get("applicable") and fw_file:
        new_content = insert_entry(
            fw_content, version, fw_result["section_name"], fw_result["entry"]
        )
        modified.append((fw_file, new_content))
        print(f"Queued update: {fw_file}")

    if addin_result.get("applicable") and addin_file:
        new_content = insert_entry(
            addin_content, version, addin_result["section_name"], addin_result["entry"]
        )
        modified.append((addin_file, new_content))
        print(f"Queued update: {addin_file}")

    if not modified:
        msg = (
            f"⚠️ The release notes generator could not determine applicable "
            f"changes for issue #{ISSUE_NUMBER}. Please add release notes manually."
        )
        print(msg)
        post_comment(msg)
        update_labels()
        return

    pr_url = create_pr(modified, version, base_branch, generated)
    print(f"PR created: {pr_url}")

    update_labels()
    post_comment(
        f"🤖 **Release notes auto-generated** — PR: {pr_url}\n\n"
        "The `needs-documentation-confirmed` label has been applied. "
        "Please review the PR for accuracy before merging."
    )
    print("Done.")


if __name__ == "__main__":
    main()
