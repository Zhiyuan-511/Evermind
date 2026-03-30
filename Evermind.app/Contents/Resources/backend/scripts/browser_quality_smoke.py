#!/usr/bin/env python3
"""
Real browser smoke test for Evermind BrowserPlugin quality features.

Runs actual Playwright Chromium against three temporary fixtures:
1. interactive website
2. simple keyboard-controlled browser game
3. interactive dashboard with filters/tabs

No mocks. No fake screenshots. This is intended as a real regression guard
for reviewer/tester browser capability.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.implementations import BrowserPlugin


WEBSITE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Evermind Website Smoke</title>
  <style>
    body { font-family: Inter, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
    header, main { padding: 24px; }
    nav { display: flex; gap: 16px; align-items: center; }
    button, a, input { font: inherit; }
    button { background: #3b82f6; color: white; border: 0; border-radius: 10px; padding: 12px 18px; cursor: pointer; }
    .panel { margin-top: 24px; padding: 20px; border-radius: 16px; background: rgba(255,255,255,0.08); }
    #success { display: none; margin-top: 16px; color: #86efac; font-weight: 700; }
    .spacer { height: 900px; }
  </style>
</head>
<body>
  <header>
    <nav>
      <a href="#features">Features</a>
      <a href="#contact">Contact</a>
      <button id="cta">Get Started</button>
    </nav>
  </header>
  <main>
    <section class="panel">
      <h1>Ship better products faster</h1>
      <p>Commercial-grade workflow orchestration.</p>
      <label>Email <input id="email" placeholder="name@company.com" /></label>
      <div id="success">Welcome aboard</div>
    </section>
    <div class="spacer"></div>
    <section id="contact" class="panel"><h2>Contact</h2></section>
  </main>
  <script>
    document.getElementById('cta').addEventListener('click', () => {
      document.getElementById('success').style.display = 'block';
      document.body.dataset.state = 'started';
    });
  </script>
</body>
</html>
"""


GAME_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Evermind Game Smoke</title>
  <style>
    body { margin: 0; font-family: Inter, sans-serif; background: #020617; color: #e2e8f0; overflow: hidden; }
    #overlay { position: absolute; inset: 0; display: grid; place-items: center; background: rgba(2,6,23,0.92); z-index: 2; }
    #startBtn { padding: 14px 22px; border: 0; border-radius: 12px; background: #22c55e; color: #04130a; font-weight: 800; cursor: pointer; }
    #hud { position: absolute; top: 16px; left: 16px; display: flex; gap: 20px; z-index: 1; font-weight: 700; }
    #arena { position: relative; width: 100vw; height: 100vh; }
    #player { position: absolute; width: 28px; height: 28px; left: 40px; top: 120px; background: #38bdf8; border-radius: 8px; box-shadow: 0 0 20px rgba(56,189,248,.7); }
  </style>
</head>
<body>
  <div id="overlay"><button id="startBtn">Start Game</button></div>
  <div id="hud"><div id="score">Score: 0</div><div id="status">Menu</div></div>
  <div id="arena"><div id="player" tabindex="0"></div></div>
  <script>
    const overlay = document.getElementById('overlay');
    const player = document.getElementById('player');
    const score = document.getElementById('score');
    const status = document.getElementById('status');
    let started = false;
    let x = 40;
    let y = 120;
    let points = 0;

    function render() {
      player.style.left = `${x}px`;
      player.style.top = `${y}px`;
      score.textContent = `Score: ${points}`;
    }

    function startGame() {
      started = true;
      overlay.style.display = 'none';
      status.textContent = 'Playing';
      document.body.dataset.state = 'playing';
      player.focus();
      render();
    }

    document.getElementById('startBtn').addEventListener('click', startGame);
    document.addEventListener('keydown', (event) => {
      if (!started) return;
      const key = event.key;
      if (key === 'ArrowRight' || key.toLowerCase() === 'd') x += 18;
      if (key === 'ArrowLeft' || key.toLowerCase() === 'a') x -= 18;
      if (key === 'ArrowUp' || key.toLowerCase() === 'w') y -= 18;
      if (key === 'ArrowDown' || key.toLowerCase() === 's') y += 18;
      if (key === ' ' || key === 'Spacebar' || key === 'Space') points += 5;
      points += 1;
      document.body.dataset.state = `playing-${points}-${x}-${y}`;
      render();
    });

    render();
  </script>
</body>
</html>
"""


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Evermind Dashboard Smoke</title>
  <style>
    body { margin: 0; font-family: Inter, sans-serif; background: #0b1220; color: #e5eefc; }
    main { padding: 24px; display: grid; gap: 18px; }
    .toolbar { display: flex; gap: 12px; flex-wrap: wrap; }
    button { border: 0; border-radius: 999px; padding: 10px 16px; font: inherit; cursor: pointer; background: rgba(255,255,255,0.09); color: inherit; }
    button.active { background: #38bdf8; color: #082032; font-weight: 800; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    .card { padding: 18px; border-radius: 18px; background: rgba(255,255,255,0.07); min-height: 120px; }
    .kpi { font-size: 42px; font-weight: 800; margin-top: 6px; }
    .chart { height: 120px; border-radius: 14px; background: linear-gradient(180deg, rgba(56,189,248,.35), rgba(56,189,248,.04)); display: grid; place-items: center; }
  </style>
</head>
<body>
  <main>
    <div class="toolbar">
      <button id="weeklyBtn" class="active">Weekly</button>
      <button id="monthlyBtn">Monthly</button>
      <button id="regionBtn">North America</button>
    </div>
    <div class="grid">
      <section class="card">
        <div>Revenue</div>
        <div id="revenue" class="kpi">$42K</div>
      </section>
      <section class="card">
        <div>Conversion</div>
        <div id="conversion" class="kpi">4.8%</div>
      </section>
      <section class="card chart">
        <div id="chartLabel">Weekly trend stable</div>
      </section>
      <section class="card">
        <div id="regionStatus">Region: North America</div>
        <div id="insight">Pipeline velocity strong</div>
      </section>
    </div>
  </main>
  <script>
    const weeklyBtn = document.getElementById('weeklyBtn');
    const monthlyBtn = document.getElementById('monthlyBtn');
    const regionBtn = document.getElementById('regionBtn');
    const revenue = document.getElementById('revenue');
    const conversion = document.getElementById('conversion');
    const chartLabel = document.getElementById('chartLabel');
    const regionStatus = document.getElementById('regionStatus');
    const insight = document.getElementById('insight');

    function activate(button) {
      for (const el of [weeklyBtn, monthlyBtn, regionBtn]) el.classList.remove('active');
      button.classList.add('active');
    }

    weeklyBtn.addEventListener('click', () => {
      activate(weeklyBtn);
      revenue.textContent = '$42K';
      conversion.textContent = '4.8%';
      chartLabel.textContent = 'Weekly trend stable';
      insight.textContent = 'Pipeline velocity strong';
      document.body.dataset.state = 'weekly-na';
    });

    monthlyBtn.addEventListener('click', () => {
      activate(monthlyBtn);
      revenue.textContent = '$168K';
      conversion.textContent = '5.6%';
      chartLabel.textContent = 'Monthly trend accelerating';
      insight.textContent = 'Expansion revenue increased';
      document.body.dataset.state = 'monthly-na';
    });

    regionBtn.addEventListener('click', () => {
      activate(regionBtn);
      regionStatus.textContent = 'Region: Europe';
      chartLabel.textContent = 'Monthly trend accelerating in Europe';
      insight.textContent = 'Enterprise demand up 18%';
      document.body.dataset.state = 'monthly-eu';
    });
  </script>
</body>
</html>
"""


def ensure(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


async def run_smoke() -> None:
    plugin = BrowserPlugin()
    try:
        with tempfile.TemporaryDirectory(prefix="evermind_browser_smoke_") as tmpdir:
            tmp_path = Path(tmpdir)
            website_path = tmp_path / "website.html"
            game_path = tmp_path / "game.html"
            dashboard_path = tmp_path / "dashboard.html"
            website_path.write_text(WEBSITE_HTML, encoding="utf-8")
            game_path.write_text(GAME_HTML, encoding="utf-8")
            dashboard_path.write_text(DASHBOARD_HTML, encoding="utf-8")

            website_url = website_path.resolve().as_uri()
            game_url = game_path.resolve().as_uri()
            dashboard_url = dashboard_path.resolve().as_uri()

            print(f"[website] navigate -> {website_url}")
            result = await plugin.execute({"action": "navigate", "url": website_url, "full_page": True}, context={})
            ensure(result.success, f"website navigate failed: {result.error}")
            print(f"  title={result.data.get('title')} state_hash={result.data.get('state_hash')}")

            result = await plugin.execute({"action": "snapshot", "limit": 10}, context={})
            ensure(result.success, f"website snapshot failed: {result.error}")
            interactive = result.data.get("snapshot", {}).get("interactive", [])
            ensure(any("Get Started" in str(item.get("text")) for item in interactive), "website snapshot did not expose CTA")
            print(f"  snapshot interactive={len(interactive)}")

            result = await plugin.execute({"action": "click", "text": "Get Started", "wait_ms": 300}, context={})
            ensure(result.success, f"website click failed: {result.error}")
            click_hash = result.data.get("state_hash")
            print(f"  click state_hash={click_hash}")

            result = await plugin.execute({"action": "wait_for", "text": "Welcome aboard", "timeout_ms": 3000}, context={})
            ensure(result.success, f"website wait_for failed: {result.error}")
            result = await plugin.execute({"action": "extract", "selector": "body"}, context={})
            ensure("Welcome aboard" in str(result.data.get("text", "")), "website success state missing after click")
            print("  website interaction verified")

            print(f"[game] navigate -> {game_url}")
            result = await plugin.execute({"action": "navigate", "url": game_url}, context={})
            ensure(result.success, f"game navigate failed: {result.error}")
            start_hash = result.data.get("state_hash")
            print(f"  start state_hash={start_hash}")

            result = await plugin.execute({"action": "snapshot", "limit": 12}, context={})
            ensure(result.success, f"game snapshot failed: {result.error}")
            ensure(any("Start Game" in str(item.get("text")) for item in result.data.get("snapshot", {}).get("interactive", [])), "game snapshot did not expose start button")

            result = await plugin.execute({"action": "click", "text": "Start Game", "wait_ms": 300}, context={})
            ensure(result.success, f"game click failed: {result.error}")
            menu_hash = result.data.get("state_hash")
            print(f"  after start click state_hash={menu_hash}")

            result = await plugin.execute({
                "action": "press_sequence",
                "keys": ["ArrowRight", "ArrowRight", "Space", "ArrowDown", "ArrowLeft"],
                "repeat": 2,
                "interval_ms": 120,
                "wait_ms": 300,
            }, context={})
            ensure(result.success, f"game press_sequence failed: {result.error}")
            gameplay_hash = result.data.get("state_hash")
            ensure(gameplay_hash != menu_hash, "gameplay did not change visible state hash after key input")
            print(f"  gameplay state_hash={gameplay_hash} keys_count={result.data.get('keys_count')}")

            result = await plugin.execute({"action": "extract", "selector": "#score"}, context={})
            ensure(result.success, f"game score extract failed: {result.error}")
            score_text = str(result.data.get("text", "")).strip()
            ensure(score_text != "Score: 0", f"score did not change after gameplay input: {score_text}")
            print(f"  score={score_text}")

            print(f"[dashboard] navigate -> {dashboard_url}")
            result = await plugin.execute({"action": "navigate", "url": dashboard_url}, context={})
            ensure(result.success, f"dashboard navigate failed: {result.error}")
            print(f"  title={result.data.get('title')} state_hash={result.data.get('state_hash')}")

            result = await plugin.execute({"action": "snapshot", "limit": 12}, context={})
            ensure(result.success, f"dashboard snapshot failed: {result.error}")
            interactive = result.data.get("snapshot", {}).get("interactive", [])
            ensure(any("Monthly" in str(item.get("text")) for item in interactive), "dashboard snapshot did not expose monthly filter")
            print(f"  snapshot interactive={len(interactive)}")

            result = await plugin.execute({"action": "click", "text": "Monthly", "wait_ms": 250}, context={})
            ensure(result.success, f"dashboard monthly click failed: {result.error}")
            monthly_hash = result.data.get("state_hash")
            print(f"  monthly state_hash={monthly_hash} state_changed={result.data.get('state_changed')}")

            result = await plugin.execute({"action": "wait_for", "text": "Monthly trend accelerating", "timeout_ms": 3000}, context={})
            ensure(result.success, f"dashboard wait_for monthly failed: {result.error}")

            result = await plugin.execute({"action": "click", "text": "North America", "wait_ms": 250}, context={})
            ensure(result.success, f"dashboard region click failed: {result.error}")
            region_hash = result.data.get("state_hash")
            ensure(region_hash != monthly_hash, "dashboard region click did not change visible state")
            print(f"  region state_hash={region_hash} state_changed={result.data.get('state_changed')}")

            result = await plugin.execute({"action": "wait_for", "text": "Region: Europe", "timeout_ms": 3000}, context={})
            ensure(result.success, f"dashboard wait_for region failed: {result.error}")

            result = await plugin.execute({"action": "extract", "selector": "body"}, context={})
            ensure(result.success, f"dashboard extract failed: {result.error}")
            body_text = str(result.data.get("text", ""))
            ensure("Region: Europe" in body_text and "$168K" in body_text, "dashboard values did not update after filter changes")
            print("  dashboard interactions verified")

            print("\nPASS: real browser smoke succeeded for website + game + dashboard interaction.")
    finally:
        await plugin.shutdown()


if __name__ == "__main__":
    asyncio.run(run_smoke())
