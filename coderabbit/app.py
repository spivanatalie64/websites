"""
CodeRabbit-like AI Code Review Service
Runs on port 9998 — self-hosted, AI-powered code review.
Wraps claude-pr-reviewer with a web UI, API, and webhook support.
"""
import os
import json
import time
import hashlib
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import git

app = FastAPI(title="CodeRabbit Self-Hosted", version="1.0.0")

BASE_DIR = Path("/home/natalie/Projects/websites/coderabbit")
REVIEWS_DIR = BASE_DIR / "reviews"
REVIEWS_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = BASE_DIR / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

# --- Data Models ---

class ReviewRequest(BaseModel):
    repo_url: str = ""
    repo_path: str = ""
    branch: str = "main"
    base_branch: str = ""
    pr_number: int = 0
    diff_content: str = ""

class ReviewResponse(BaseModel):
    id: str
    status: str
    summary: str
    findings: list
    overall_recommendation: str
    timestamp: str

# --- In-memory store ---
reviews_store = {}

# --- Templates ---
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CodeRabbit — Self-Hosted AI Code Review</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
        header { display: flex; align-items: center; gap: 1rem; margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid #30363d; }
        header h1 { font-size: 1.8rem; color: #58a6ff; }
        header .subtitle { color: #8b949e; font-size: 0.9rem; }
        .logo { font-size: 2rem; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
        .card h2 { color: #58a6ff; margin-bottom: 1rem; font-size: 1.2rem; }
        .card h3 { color: #c9d1d9; margin-bottom: 0.5rem; }
        label { display: block; color: #8b949e; margin-bottom: 0.3rem; font-size: 0.85rem; }
        input, textarea, select { width: 100%; padding: 0.6rem; margin-bottom: 1rem; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-family: inherit; }
        textarea { font-family: 'Courier New', monospace; font-size: 0.85rem; }
        button { background: #238636; color: white; border: none; padding: 0.6rem 1.5rem; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
        button:hover { background: #2ea043; }
        button.danger { background: #da3633; }
        button.danger:hover { background: #f85149; }
        .review { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; }
        .review h3 { color: #58a6ff; }
        .review .meta { color: #8b949e; font-size: 0.85rem; margin-bottom: 0.5rem; }
        .finding { border-left: 3px solid; padding: 0.8rem; margin: 0.5rem 0; background: #0d1117; border-radius: 0 6px 6px 0; }
        .finding.critical { border-color: #f85149; }
        .finding.high { border-color: #d29922; }
        .finding.medium { border-color: #58a6ff; }
        .finding.low { border-color: #8b949e; }
        .finding .severity { font-weight: bold; text-transform: uppercase; font-size: 0.75rem; }
        .finding .severity.critical { color: #f85149; }
        .finding .severity.high { color: #d29922; }
        .finding .severity.medium { color: #58a6ff; }
        .finding .severity.low { color: #8b949e; }
        .finding .file { color: #58a6ff; font-family: monospace; font-size: 0.85rem; }
        .finding .lines { color: #8b949e; font-size: 0.8rem; }
        .finding .desc { margin: 0.5rem 0; }
        .finding .fix { background: #0d1117; border: 1px solid #30363d; border-radius: 4px; padding: 0.5rem; font-family: monospace; font-size: 0.8rem; white-space: pre-wrap; margin-top: 0.5rem; }
        .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 12px; font-size: 0.75rem; font-weight: bold; }
        .badge.approve { background: #1b4a1b; color: #3fb950; }
        .badge.changes { background: #4a1b1b; color: #f85149; }
        .badge.comment { background: #1b2a4a; color: #58a6ff; }
        .nav { display: flex; gap: 1rem; margin-bottom: 1rem; }
        .nav a { color: #58a6ff; text-decoration: none; padding: 0.3rem 0.8rem; border-radius: 4px; }
        .nav a:hover { background: #1f2937; }
        .nav a.active { background: #1f2937; color: #f0f6fc; }
        .empty { text-align: center; padding: 3rem; color: #8b949e; }
        .empty .big { font-size: 3rem; margin-bottom: 1rem; }
        .tabs { display: flex; gap: 0; margin-bottom: 1rem; border-bottom: 1px solid #30363d; }
        .tabs button { background: none; color: #8b949e; border: none; padding: 0.5rem 1rem; border-bottom: 2px solid transparent; border-radius: 0; cursor: pointer; }
        .tabs button.active { color: #f0f6fc; border-bottom-color: #f78166; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .status-bar { display: flex; gap: 1rem; margin-bottom: 1rem; padding: 0.5rem; background: #161b22; border-radius: 6px; font-size: 0.85rem; }
        .status-bar .stat { color: #8b949e; }
        .status-bar .stat span { color: #c9d1d9; font-weight: bold; }
        .toast { position: fixed; bottom: 2rem; right: 2rem; background: #238636; color: white; padding: 0.8rem 1.5rem; border-radius: 6px; display: none; }
        .toast.error { background: #da3633; }
        .spinner { display: inline-block; width: 1rem; height: 1rem; border: 2px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 0.5rem; vertical-align: middle; }
        @keyframes spin { to { transform: rotate(360deg); } }
        pre { background: #0d1117; border: 1px solid #30363d; border-radius: 4px; padding: 1rem; overflow-x: auto; font-size: 0.8rem; }
        .review-header { display: flex; justify-content: space-between; align-items: center; }
        .badge-rec { display: inline-block; padding: 0.25rem 0.75rem; border-radius: 20px; font-size: 0.8rem; font-weight: bold; }
        .badge-rec.approve { background: #1b4a1b; color: #3fb950; }
        .badge-rec.changes { background: #4a1b1b; color: #f85149; }
        .badge-rec.comment { background: #1b2a4a; color: #58a6ff; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo">🐰</div>
            <div>
                <h1>CodeRabbit</h1>
                <div class="subtitle">Self-Hosted AI Code Review — Port 9998</div>
            </div>
        </header>

        <div class="status-bar">
            <div class="stat">Reviews: <span id="review-count">0</span></div>
            <div class="stat">Status: <span id="server-status" style="color:#3fb950;">● Running</span></div>
            <div class="stat">Engine: <span>claude-pr-reviewer</span></div>
        </div>

        <div class="tabs">
            <button class="active" onclick="switchTab('review')">New Review</button>
            <button onclick="switchTab('history')">Review History</button>
            <button onclick="switchTab('api')">API Docs</button>
            <button onclick="switchTab('settings')">Settings</button>
        </div>

        <!-- New Review Tab -->
        <div id="tab-review" class="tab-content active">
            <div class="card">
                <h2>Review Code</h2>
                <div class="nav">
                    <button class="active" onclick="switchMode('diff')">Paste Diff</button>
                    <button onclick="switchMode('local')">Local Repo</button>
                    <button onclick="switchMode('pr')">GitHub PR</button>
                </div>

                <!-- Diff Mode -->
                <div id="mode-diff">
                    <label for="diff-input">Paste a unified diff (git diff output):</label>
                    <textarea id="diff-input" rows="15" placeholder="Paste git diff output here..."></textarea>
                    <button onclick="submitDiff()">Review Diff</button>
                </div>

                <!-- Local Mode -->
                <div id="mode-local" style="display:none;">
                    <label for="repo-path">Repository Path:</label>
                    <input type="text" id="repo-path" placeholder="/home/natalie/Projects/websites" value="/home/natalie/Projects/websites">
                    <label for="base-branch">Base Branch (optional):</label>
                    <input type="text" id="base-branch" placeholder="main" value="main">
                    <label for="target-branch">Target Branch (optional):</label>
                    <input type="text" id="target-branch" placeholder="HEAD">
                    <button onclick="submitLocal()">Review Local Changes</button>
                </div>

                <!-- PR Mode -->
                <div id="mode-pr" style="display:none;">
                    <label for="pr-url">GitHub PR URL or Number:</label>
                    <input type="text" id="pr-url" placeholder="https://github.com/owner/repo/pull/123 or just 123">
                    <label for="pr-repo">Repository (owner/repo):</label>
                    <input type="text" id="pr-repo" placeholder="owner/repo">
                    <button onclick="submitPR()">Review PR</button>
                </div>
            </div>

            <div id="loading" style="display:none;" class="card">
                <p><span class="spinner"></span> Reviewing code... This may take a moment.</p>
            </div>

            <div id="result" style="display:none;"></div>
        </div>

        <!-- History Tab -->
        <div id="tab-history" class="tab-content">
            <div class="card">
                <h2>Review History</h2>
                <div id="history-list">
                    <div class="empty"><div class="big">📋</div><p>No reviews yet. Submit your first review!</p></div>
                </div>
            </div>
        </div>

        <!-- API Tab -->
        <div id="tab-api" class="tab-content">
            <div class="card">
                <h2>API Documentation</h2>
                <h3>POST /api/review</h3>
                <p>Submit code for review.</p>
                <pre>{
  "diff_content": "string (unified diff)",
  "repo_url": "string (optional)",
  "repo_path": "string (optional)",
  "branch": "string (default: main)",
  "base_branch": "string (optional)",
  "pr_number": "int (optional)"
}</pre>
                <h3>GET /api/reviews</h3>
                <p>List all reviews.</p>
                <h3>GET /api/reviews/{id}</h3>
                <p>Get a specific review.</p>
                <h3>POST /api/webhook/github</h3>
                <p>GitHub webhook endpoint for automatic PR reviews.</p>
                <p>Full API docs: <a href="/docs" style="color:#58a6ff;">/docs</a> (Swagger UI)</p>
            </div>
        </div>

        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content">
            <div class="card">
                <h2>Settings</h2>
                <label for="api-key">Anthropic API Key:</label>
                <input type="password" id="api-key" placeholder="sk-ant-..." value="">
                <label for="model">Model:</label>
                <select id="model">
                    <option value="claude-sonnet-4-20250514">claude-sonnet-4-20250514</option>
                    <option value="claude-3-5-sonnet-20241022">claude-3-5-sonnet-20241022</option>
                    <option value="claude-3-opus-20240229">claude-3-opus-20240229</option>
                </select>
                <label for="gh-token">GitHub Token (for PR review):</label>
                <input type="password" id="gh-token" placeholder="ghp_...">
                <button onclick="saveSettings()">Save Settings</button>
            </div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let reviews = {};

        function switchTab(tab) {
            document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelector(`.tabs button:nth-child(${tab === 'review' ? 1 : tab === 'history' ? 2 : tab === 'api' ? 3 : 4})`).classList.add('active');
            document.getElementById(`tab-${tab}`).classList.add('active');
        }

        function switchMode(mode) {
            document.querySelectorAll('#tab-review .nav button').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('[id^="mode-"]').forEach(m => m.style.display = 'none');
            document.getElementById(`mode-${mode}`).style.display = 'block';
            event.target.classList.add('active');
        }

        function showToast(msg, isError = false) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast' + (isError ? ' error' : '');
            t.style.display = 'block';
            setTimeout(() => t.style.display = 'none', 4000);
        }

        async function submitDiff() {
            const diff = document.getElementById('diff-input').value;
            if (!diff.trim()) { showToast('Please paste a diff first.', true); return; }
            await doReview({ diff_content: diff });
        }

        async function submitLocal() {
            const repo_path = document.getElementById('repo-path').value;
            const base = document.getElementById('base-branch').value;
            const target = document.getElementById('target-branch').value;
            await doReview({ repo_path, base_branch: base, branch: target });
        }

        async function submitPR() {
            const pr_url = document.getElementById('pr-url').value;
            const repo = document.getElementById('pr-repo').value;
            await doReview({ pr_url, repo_url: repo });
        }

        async function doReview(data) {
            document.getElementById('loading').style.display = 'block';
            document.getElementById('result').style.display = 'none';
            try {
                const resp = await fetch('/api/review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                const result = await resp.json();
                if (!resp.ok) { showToast(result.detail || 'Review failed', true); return; }
                document.getElementById('result').style.display = 'block';
                renderReview(result);
                loadHistory();
                showToast('Review complete!');
            } catch (e) {
                showToast('Error: ' + e.message, true);
            } finally {
                document.getElementById('loading').style.display = 'none';
            }
        }

        async function doReview(data) {
            document.getElementById('loading').style.display = 'block';
            document.getElementById('result').style.display = 'none';
            try {
                const resp = await fetch('/api/review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                const result = await resp.json();
                if (!resp.ok) { showToast(result.detail || 'Review failed', true); return; }
                document.getElementById('result').style.display = 'block';
                renderReview(result);
                loadHistory();
                showToast('Review complete!');
            } catch (e) {
                showToast('Error: ' + e.message, true);
            } finally {
                document.getElementById('loading').style.display = 'none';
            }
        }

        function renderReview(r) {
            const el = document.getElementById('result');
            const recClass = r.overall_recommendation || 'comment';
            let findingsHtml = '';
            if (r.findings && r.findings.length) {
                r.findings.forEach(f => {
                    const sev = (f.severity || 'medium').toLowerCase();
                    findingsHtml += `
                        <div class="finding ${sev}">
                            <div class="severity ${sev}">${sev}</div>
                            <div class="file">${f.file_path || 'unknown'}</div>
                            <div class="lines">Lines ${f.start_line || '?'}-${f.end_line || '?'}</div>
                            <div class="desc"><strong>${f.title || 'Finding'}</strong></div>
                            <div class="desc">${f.description || ''}</div>
                            ${f.suggested_fix ? `<div class="fix">${f.suggested_fix}</div>` : ''}
                        </div>
                    `;
                });
            } else {
                findingsHtml = '<p style="color:#3fb950;">✅ No issues found!</p>';
            }
            el.innerHTML = `
                <div class="review">
                    <div class="review-header">
                        <h3>Review #${r.id}</h3>
                        <span class="badge-rec ${recClass}">${r.overall_recommendation || 'comment'}</span>
                    </div>
                    <div class="meta">${r.timestamp} | ${r.findings ? r.findings.length : 0} findings</div>
                    <p>${r.summary || 'No summary provided.'}</p>
                    <h3 style="margin-top:1rem;">Findings</h3>
                    ${findingsHtml}
                </div>
            `;
        }

        async function loadHistory() {
            try {
                const resp = await fetch('/api/reviews');
                const data = await resp.json();
                const el = document.getElementById('history-list');
                document.getElementById('review-count').textContent = data.length;
                if (!data.length) {
                    el.innerHTML = '<div class="empty"><div class="big">📋</div><p>No reviews yet.</p></div>';
                    return;
                }
                let html = '';
                data.slice().reverse().forEach(r => {
                    const recClass = (r.overall_recommendation || 'comment').toLowerCase();
                    html += \`
                        <div class="review" onclick="viewReview('\${r.id}')" style="cursor:pointer;">
                            <div class="review-header">
                                <h3>Review #\${r.id}</h3>
                                <span class="badge-rec \${recClass}">\${r.overall_recommendation || 'comment'}</span>
                            </div>
                            <div class="meta">\${r.timestamp} | \${r.findings ? r.findings.length : 0} findings</div>
                            <p>\${(r.summary || '').substring(0, 200)}\${(r.summary || '').length > 200 ? '...' : ''}</p>
                        </div>
                    \`;
                });
                el.innerHTML = html;
            } catch (e) { console.error(e); }
        }

        async function viewReview(id) {
            try {
                const resp = await fetch('/api/reviews/' + id);
                const r = await resp.json();
                renderReview(r);
                switchTab('review');
            } catch (e) { showToast('Error loading review', true); }
        }

        async function saveSettings() {
            const data = {
                api_key: document.getElementById('api-key').value,
                model: document.getElementById('model').value,
                gh_token: document.getElementById('gh-token').value
            };
            try {
                const resp = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                if (resp.ok) showToast('Settings saved!');
                else showToast('Failed to save settings', true);
            } catch (e) { showToast('Error: ' + e.message, true); }
        }

        loadHistory();
    </script>
</body>
</html>"""
