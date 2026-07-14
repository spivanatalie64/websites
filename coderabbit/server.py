"""
CodeRabbit-like AI Code Review Service
Runs on port 9998 — self-hosted, AI-powered code review.
Wraps claude-pr-reviewer with a web UI, API, and webhook support.
"""
import os
import json
import time
import uuid
import subprocess
import tempfile
import shutil
import re
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI(title="CodeRabbit Self-Hosted", version="1.0.0", docs_url="/docs")

BASE_DIR = Path("/home/natalie/Projects/websites/coderabbit")
REVIEWS_DIR = BASE_DIR / "reviews"
REVIEWS_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

# Settings file
SETTINGS_FILE = BASE_DIR / "settings.json"

# In-memory store
reviews_store = {}

# --- Models ---

from pydantic import BaseModel
from typing import Optional

class ReviewRequest(BaseModel):
    diff_content: str = ""
    repo_url: str = ""
    repo_path: str = ""
    branch: str = "HEAD"
    base_branch: str = "main"
    pr_number: int = 0
    pr_url: str = ""

class SettingsData(BaseModel):
    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    gh_token: str = ""

# --- Review Engine ---

def run_claude_review(diff_content: str) -> dict:
    """Run claude-pr-reviewer on diff content and parse the output."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as f:
        f.write(diff_content)
        diff_path = f.name

    try:
        result = subprocess.run(
            ['claude-pr-reviewer', 'review-diff', '--format', 'json'],
            input=diff_content,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, 'PATH': '/home/natalie/.local/bin:' + os.environ.get('PATH', '')}
        )
        if result.returncode != 0:
            # Fallback: try to parse any JSON in stderr
            stderr = result.stderr
            # Try to find JSON in output
            import re
            json_match = re.search(r'\{.*\}', result.stdout + result.stderr, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except:
                    pass
            return {
                "summary": f"Review completed with warnings. stderr: {stderr[:500] if stderr else 'none'}",
                "overall_recommendation": "comment",
                "findings": []
            }
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "summary": result.stdout[:1000] if result.stdout else "Review completed.",
                "overall_recommendation": "comment",
                "findings": []
            }
    except subprocess.TimeoutExpired:
        return {"summary": "Review timed out after 120s.", "overall_recommendation": "comment", "findings": []}
    except Exception as e:
        return {"summary": f"Review error: {str(e)}", "overall_recommendation": "comment", "findings": []}


def get_local_diff(repo_path: str, base_branch: str = "main", target_branch: str = "HEAD") -> str:
    """Get git diff for a local repository."""
    try:
        result = subprocess.run(
            ['git', 'diff', f'{base_branch}..{target_branch}'],
            capture_output=True, text=True, timeout=30,
            cwd=repo_path
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        # Fallback: working tree diff
        result = subprocess.run(
            ['git', 'diff'],
            capture_output=True, text=True, timeout=30,
            cwd=repo_path
        )
        return result.stdout
    except Exception as e:
        return f""


# --- API Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(TEMPLATES_DIR / "index.html") as f:
        return HTMLResponse(f.read())

@app.post("/api/review")
async def create_review(req: Request):
    data = await req.json()
    diff_content = data.get("diff_content", "")
    repo_path = data.get("repo_path", "")
    base_branch = data.get("base_branch", "main")
    branch = data.get("branch", "HEAD")
    pr_url = data.get("pr_url", "")

    # Get diff content if not provided directly
    if not diff_content and repo_path:
        diff_content = get_local_diff(repo_path, base_branch, branch)

    if not diff_content and pr_url:
        # Try to fetch PR diff from GitHub
        import re
        m = re.search(r'github\.com/([^/]+/[^/]+)/pull/(\d+)', pr_url)
        if m:
            repo = m.group(1)
            pr_num = m.group(2)
            try:
                resp = httpx.get(f"https://api.github.com/repos/{repo}/pulls/{pr_num}", timeout=15)
                if resp.status_code == 200:
                    pr_data = resp.json()
                    diff_url = pr_data.get("diff_url", "")
                    if diff_url:
                        diff_resp = httpx.get(diff_url, timeout=30)
                        diff_content = diff_resp.text
            except:
                pass

    if not diff_content:
        raise HTTPException(status_code=400, detail="No diff content provided and could not generate one.")

    # Run the review
    review_result = run_review(diff_content)

    # Store the review
    review_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    timestamp = datetime.now().isoformat()
    review_data = {
        "id": review_id,
        "status": "completed",
        "summary": review_result.get("summary", "Review completed."),
        "findings": review_result.get("findings", []),
        "overall_recommendation": review_result.get("overall_recommendation", "comment"),
        "timestamp": timestamp
    }
    reviews_store[review_id] = review_data

    # Save to disk
    with open(REVIEWS_DIR / f"{review_id}.json", "w") as f:
        json.dump(review_data, f, indent=2)

    return review_data


@app.get("/api/reviews")
async def list_reviews():
    return list(reviews_store.values())


@app.get("/api/reviews/{review_id}")
async def get_review(review_id: str):
    if review_id not in reviews_store:
        raise HTTPException(status_code=404, detail="Review not found")
    return reviews_store[review_id]


@app.post("/api/settings")
async def save_settings(req: Request):
    data = await req.json()
    settings = {}
    if data.get("api_key"):
        settings["ANTHROPIC_API_KEY"] = data["api_key"]
    if data.get("model"):
        settings["MODEL"] = data["model"]
    if data.get("gh_token"):
        settings["GITHUB_TOKEN"] = data["gh_token"]
    with open(BASE_DIR / "settings.json", "w") as f:
        json.dump(settings, f, indent=2)
    # Apply to environment
    for k, v in settings.items():
        os.environ[k] = v
    return {"status": "ok"}


@app.post("/api/webhook/github")
async def github_webhook(req: Request):
    """GitHub webhook endpoint for automatic PR review."""
    data = await req.json()
    action = data.get("action", "")
    if action in ["opened", "synchronize"] and "pull_request" in data:
        pr = data["pull_request"]
        repo_full = data["repository"]["full_name"]
        pr_number = pr["number"]
        diff_url = pr.get("diff_url", "")
        if diff_url:
            resp = httpx.get(diff_url, timeout=30)
            diff_content = resp.text
            review_result = run_review(diff_content)
            review_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
            timestamp = datetime.now().isoformat()
            review_data = {
                "id": review_id,
                "status": "completed",
                "summary": review_result.get("summary", "Review completed."),
                "findings": review_result.get("findings", []),
                "overall_recommendation": review_result.get("overall_recommendation", "comment"),
                "timestamp": timestamp,
                "repo": repo_full,
                "pr_number": data.get("pull_request", {}).get("number", 0)
            }
            reviews_store[review_id] = review_data
            with open(REVIEWS_DIR / f"{review_id}.json", "w") as f:
                json.dump(review_data, f, indent=2)
            return {"status": "ok", "review_id": review_id}
    return {"status": "ignored", "action": action}


@app.get("/health")
async def health():
    return {"status": "ok", "engine": "claude-pr-reviewer", "reviews": len(reviews_store)}


def run_review(diff_content: str) -> dict:
    """Run claude-pr-reviewer on diff content and parse the output."""
    try:
        result = subprocess.run(
            ['claude-pr-reviewer', 'review-diff', '--format', 'json'],
            input=diff_content,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                'PATH': '/home/natalie/.local/bin:' + os.environ.get('PATH', ''),
                'HOME': '/home/natalie'
            }
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
        # Try to find JSON in output
        import re
        json_match = re.search(r'(\{.*\})', result.stdout + result.stderr, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except:
                pass
        return {
            "summary": (result.stdout or result.stderr or "Review completed.")[:1000],
            "overall_recommendation": "comment",
            "findings": []
        }
    except subprocess.TimeoutExpired:
        return {"summary": "Review timed out after 120s.", "overall_recommendation": "comment", "findings": []}
    except Exception as e:
        return {"summary": f"Review error: {str(e)}", "overall_recommendation": "comment", "findings": []}


# Load existing reviews on startup
def load_reviews():
    for f in REVIEWS_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
                reviews_store[data["id"]] = data
        except:
            pass

load_reviews()
