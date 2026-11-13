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

// 5. Move the caption by drag (downwards stays inside the safe area) and
// confirm the human edit is persisted as a project revision.
const range = await page.$(".transport input[type=range]");
const cap = await page.$(".caption-block");
check("字幕块可拖动（画布内）", cap !== null);
const projectId = await page.$eval(".topbar select", (el) => el.value);
if (cap) {
  const before = await page.evaluate(async (pid) => {
    const p = await (await fetch(`/api/projects/${pid}`)).json();
    return p.revCounter;
  }, projectId);
  const box = await cap.boundingBox();
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  // Small screen-space drag: at ~0.73 stage scale this is ~8 video px, safely
  // inside the bottom safe margin. Larger drags may be rejected by validation
  // (that is correct behaviour, covered separately by the 409 path).
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx, cy - 3, { steps: 3 });
  await page.mouse.move(cx + 4, cy - 6, { steps: 3 });
  await page.mouse.up();
  await sleep(1500);
  const after = await page.evaluate(async (pid) => {
    const p = await (await fetch(`/api/projects/${pid}`)).json();
    return {
      rev: p.revCounter,
      placement: p.placements[Object.keys(p.placements)[0]] || null,
    };
  }, projectId);
  check("拖动字幕保存为工程修订", after.rev > before, `修订 ${before} → ${after.rev}`);
  check("手动位置写入 placements", after.placement !== null && after.placement.mode === "manual",
    JSON.stringify(after.placement && { x: after.placement.x, y: after.placement.y, mode: after.placement.mode }));
}

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
