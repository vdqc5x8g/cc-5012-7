// Browser acceptance check for the sign-caption editing page.
// Run: NODE_PATH=/tmp/uicheck/node_modules \
//   LD_LIBRARY_PATH=/tmp/debs/root/lib/aarch64-linux-gnu:/tmp/debs/root/usr/lib/aarch64-linux-gnu \
//   node scripts/ui_check.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

// Puppeteer is an optional dev tool; resolve it from a harness install dir when
// the repo itself has not installed it.
const require = createRequire(
  process.env.PUPPETEER_BASE ? process.env.PUPPETEER_BASE + "/package.json" : import.meta.url
);
const puppeteer = require("puppeteer");

const BASE = process.env.SIGSUB_URL || "http://127.0.0.1:8787";
const OUT = process.env.SIGSUB_SHOTS || "/tmp/signsub-shots";
mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(name, cond, detail = "") {
  results.push({ name, ok: !!cond, detail });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

const browser = await puppeteer.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
const consoleErrors = [];
page.on("console", (m) => {
  // Expected 409s come from deliberately conflicting drag attempts; only
  // unexpected resource failures count.
  if (m.type() === "error" && !/status of 409/.test(m.text())) {
    consoleErrors.push(m.text());
  }
});
page.on("pageerror", (e) => consoleErrors.push(String(e)));

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });

// 1. The editing page is the graphical entry point.
await page.waitForSelector(".app .stage-frame", { timeout: 20000 });
await page.waitForSelector(".candidate", { timeout: 15000 });
check("手语字幕编辑页加载", true);
check("存在时间线编辑区", await page.$(".panel.left") !== null);
check("存在候选位置与代价区", await page.$(".panel.bottom") !== null);
check("存在结果与检查区", await page.$(".panel.right") !== null);

// 2. Layout regions and captions are drawn from backend results.
const regionBoxes = await page.$$eval(".region-box", (els) => els.length);
check("人物保护区/重要区域叠层", regionBoxes >= 4, `${regionBoxes} 个区域`);
const captionBlocks = await page.$$eval(".caption-block", (els) => els.length);
check("实际字形墨迹字幕块", captionBlocks >= 1, `${captionBlocks} 个`);

// 3. Result tab reports actual protection/occlusion computation.
const resultText = await page.$eval(".panel.right", (el) => el.innerText);
check("结果区显示人物保护区", resultText.includes("人物保护区"));
check("结果区显示遮挡冲突计数", /遮挡冲突\s*\d+\s*处/.test(resultText));
check("结果区提示人工标注/未自动识别", resultText.includes("未做自动识别") || resultText.includes("人工标注"));

// 4. Candidates expose cost and conflict explanation.
const bottomText = await page.$eval(".panel.bottom", (el) => el.innerText);
check("候选方案展示", /候选\s*1/.test(bottomText));
check("代价展示", bottomText.includes("代价"));
check("采用按钮", (await page.$$(".candidate button")).length > 0);

// 4b. 播放手语：点击播放后时间轴与视频帧同步推进。
const playBtn = await page.$(".transport button");
await playBtn.click();
await sleep(1200);
const t1 = await page.$eval(".transport .time", (el) => el.textContent);
const vT1 = await page.$eval(".stage-frame video", (v) => v.currentTime);
await sleep(700);
const t2 = await page.$eval(".transport .time", (el) => el.textContent);
const vT2 = await page.$eval(".stage-frame video", (v) => v.currentTime);
await page.$eval(".transport button", (b) => b.click()); // pause
check("播放手语视频时间推进", t1 !== t2 && vT2 > vT1, `${t1}→${t2} · video ${vT1.toFixed(2)}→${vT2.toFixed(2)}`);

// 5. 变更前快照：人物保护区与遮挡来自真实 API（界面不得用固定数据冒充）。
const projectId = await page.$eval(".topbar select", (el) => el.value);
const snapBefore = await page.evaluate(async (pid) => {
  const p = await (await fetch(`/api/projects/${pid}`)).json();
  const cueId = p.cues[0].id;
  const lay = p.layout.perCue[cueId];
  return {
    rev: p.revCounter,
    cueId,
    ink: lay.geometry ? lay.geometry.ink : null,
    conflicts: lay.geometry ? lay.geometry.conflicts.length : null,
    placementCount: Object.keys(p.placements).length,
    regions: p.regions.map((r) => [r.id, ...r.rect.map(Math.round)]),
    activeCount: p.regions.filter((r) => r.start <= 0.53 && 0.53 < r.end).length,
  };
}, projectId);
const uiConflict = await page.$eval(".panel.right", (el) => {
  const m = el.innerText.match(/遮挡冲突\s*(\d+)\s*处/);
  return m ? Number(m[1]) : null;
});
check("界面遮挡计数与 API 一致（非固定数据）",
  uiConflict !== null && uiConflict === snapBefore.conflicts,
  `UI ${uiConflict} vs API ${snapBefore.conflicts}`);

// 5b. 硬约束：把字幕拖进人物保护区必须被拒绝（保护手语区域仍是硬约束）。
const stageBox = await (await page.$(".stage-frame")).boundingBox();
const scale = stageBox.width / 1280;
const cap = await page.$(".caption-block");
check("字幕块可拖动（画布内）", cap !== null);
const capBox = await cap.boundingBox();
const revBeforeBadDrag = snapBefore.rev;
await page.mouse.move(capBox.x + capBox.width / 2, capBox.y + capBox.height / 2);
await page.mouse.down();
await page.mouse.move(stageBox.x + 410 * scale, stageBox.y + 330 * scale, { steps: 6 });
await page.mouse.up();
await sleep(1200);
const badDrag = await page.evaluate(async (pid) => {
  const p = await (await fetch(`/api/projects/${pid}`)).json();
  return { rev: p.revCounter, placements: Object.keys(p.placements).length };
}, projectId);
const badToast = await page.$(".toast.bad");
check("拖入人物保护区被拒绝（硬约束）",
  badDrag.rev === revBeforeBadDrag && badDrag.placements === snapBefore.placementCount && badToast !== null,
  `修订 ${revBeforeBadDrag}→${badDrag.rev} · placements ${snapBefore.placementCount}→${badDrag.placements}`);

// 5c. 实际移动字幕到有效位置并保存（界面按钮调用真实布局实现）。
// 拖拽移动的是当前字幕块（保持其行数/墨迹尺寸）；用真实 validate API 探测
// 一个对该块有效且明显离开原位的目标位置（不伪造避让数据）。
const probe = await page.evaluate(async (pid, cid) => {
  const p = await (await fetch(`/api/projects/${pid}`)).json();
  const geo = p.layout.perCue[cid].geometry;
  const lines = geo.lines.map((l) => l.text);
  const s = p.layout.settings;
  const W = p.video.width, H = p.video.height;
  for (let y = s.safe_margin + 5; y + geo.ink[3] <= H - s.safe_margin - 5; y += 40) {
    for (let x = s.safe_margin + 5; x + geo.ink[2] <= W - s.safe_margin - 5; x += 40) {
      if (Math.abs(x - geo.ink[0]) < 40 && Math.abs(y - geo.ink[1]) < 40) continue;
      const v = await (await fetch(`/api/projects/${pid}/cues/${cid}/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x, y, fontPx: geo.fontPx, lines }),
      })).json();
      if (v.valid) return { x, y, fontPx: geo.fontPx, lines };
    }
  }
  return null;
}, projectId, snapBefore.cueId);
const target = probe;
check("存在可移动到的有效候选位置", !!target,
  target ? `目标 (${Math.round(target.x)}, ${Math.round(target.y)})` : "无");
if (target) {
  const cap2 = await page.$(".caption-block");
  const box2 = await cap2.boundingBox();
  await page.mouse.move(box2.x + box2.width / 2, box2.y + box2.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    stageBox.x + (target.x + snapBefore.ink[2] / 2) * scale,
    stageBox.y + (target.y + snapBefore.ink[3] / 2) * scale,
    { steps: 8 }
  );
  await page.mouse.up();
  await sleep(1500);
}
const snapAfter = await page.evaluate(async (pid, cid) => {
  const p = await (await fetch(`/api/projects/${pid}`)).json();
  const lay = p.layout.perCue[cid];
  const pl = p.placements[cid];
  return {
    rev: p.revCounter,
    ink: lay.geometry ? lay.geometry.ink : null,
    conflicts: lay.geometry ? lay.geometry.conflicts.length : null,
    placement: pl ? { x: pl.x, y: pl.y, mode: pl.mode } : null,
    regions: p.regions.map((r) => [r.id, ...r.rect.map(Math.round)]),
  };
}, projectId, snapBefore.cueId);
check("移动字幕保存为工程修订", target && snapAfter.rev > revBeforeBadDrag,
  `修订 ${revBeforeBadDrag} → ${snapAfter.rev}`);
check("手动位置写入 placements",
  target && snapAfter.placement && snapAfter.placement.mode === "manual"
  && Math.abs(snapAfter.placement.x - target.x) <= 2 && Math.abs(snapAfter.placement.y - target.y) <= 2,
  JSON.stringify(snapAfter.placement));

// 5d. 变更前后对照：人物保护区不变，遮挡按新位置真实重算，界面与 API 一致。
const regionsSame = JSON.stringify(snapBefore.regions) === JSON.stringify(snapAfter.regions);
const moved = target && snapAfter.ink
  && (Math.abs(snapAfter.ink[0] - snapBefore.ink[0]) > 20 || Math.abs(snapAfter.ink[1] - snapBefore.ink[1]) > 20);
const uiConflictAfter = await page.$eval(".panel.right", (el) => {
  const m = el.innerText.match(/遮挡冲突\s*(\d+)\s*处/);
  return m ? Number(m[1]) : null;
});
check("变更前后人物保护区一致", regionsSame);
check("字幕墨迹位置已实际移动", !!moved,
  `${JSON.stringify(snapBefore.ink)} → ${JSON.stringify(snapAfter.ink)}`);
check("变更后遮挡重算且界面与 API 一致",
  uiConflictAfter !== null && uiConflictAfter === snapAfter.conflicts,
  `UI ${uiConflictAfter} vs API ${snapAfter.conflicts}`);

const range = await page.$(".transport input[type=range]");

// 5b. Adopt a candidate and confirm the revision counter advances again.
const beforeAdopt = await page.evaluate(async (pid) => {
  const p = await (await fetch(`/api/projects/${pid}`)).json();
  return p.revCounter;
}, projectId);
const adoptBtn = await page.$(".candidate button.primary");
if (adoptBtn) {
  await adoptBtn.click();
  await sleep(1500);
}
const afterAdopt = await page.evaluate(async (pid) => {
  const p = await (await fetch(`/api/projects/${pid}`)).json();
  const pl = Object.values(p.placements)[0];
  return { rev: p.revCounter, mode: pl ? pl.mode : null };
}, projectId);
check("采用候选保存为工程修订", adoptBtn !== null && afterAdopt.rev > beforeAdopt,
  `修订 ${beforeAdopt} → ${afterAdopt.rev}`);

// 6. Switch to a timestamp inside the fully occluded interval (7.0-8.0s).
if (range) {
  await page.evaluate((el) => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, "value").set;
    setter.call(el, "7.2");
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }, range);
  await sleep(600);
  const t = await page.$eval(".panel.right", (el) => el.innerText);
  check("全画面占用时段提示遮挡/无解", /无解|部分遮挡|完全遮挡|不渲染/.test(t));
}

// 7. 逐帧检查 tab shows source + burned composite images.
await page.click(".tabs button:nth-child(3)");
await sleep(1500);
await page.screenshot({ path: `${OUT}/check-tab.png` });
const imgs = await page.$$eval(".panel.right img", (els) =>
  els.map((e) => ({ src: e.src, w: e.naturalWidth, h: e.naturalHeight })));
check("逐帧检查含源帧与烧录合成帧", imgs.length >= 2 && imgs.every((i) => i.w > 0),
  JSON.stringify(imgs.map((i) => `${i.w}x${i.h}`)));

// 8. Export tab exposes the verification entry point.
await page.click(".tabs button:nth-child(4)");
await sleep(400);
const exportText = await page.$eval(".panel.right", (el) => el.innerText);
check("导出核验入口", exportText.includes("导出字幕视频并核验"));

// Capture main screens for review.
await page.click(".tabs button:nth-child(1)");
await sleep(500);
await page.screenshot({ path: `${OUT}/main.png` });

check("无前端运行时错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed; shots: ${OUT}`);
process.exit(failed.length ? 1 : 0);
