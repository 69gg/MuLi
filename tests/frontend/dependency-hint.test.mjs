import test from "node:test";
import assert from "node:assert/strict";
import { dependencyHint } from "../../web/management.mjs";

test("available capability has no dependency hint", () => {
  assert.equal(dependencyHint({ available: true, reason: "" }), "");
  assert.equal(dependencyHint(null), "");
});

test("missing tool appends the exact install command", () => {
  const text = dependencyHint({
    available: false,
    reason: "Wayland 会话录屏需要 wf-recorder（缺少：wf-recorder）",
    install_hint: { command: "pacman -S --needed --noconfirm wf-recorder" },
  });
  assert.equal(
    text,
    "Wayland 会话录屏需要 wf-recorder（缺少：wf-recorder）；可安装：pacman -S --needed --noconfirm wf-recorder",
  );
});

test("unavailable capability without a command keeps the reason and note", () => {
  assert.equal(
    dependencyHint({
      available: false,
      reason: "未检测到图形会话",
      install_hint: { command: null, note: "请手动安装" },
    }),
    "未检测到图形会话；请手动安装",
  );
  assert.equal(dependencyHint({ available: false, reason: "未检测到图形会话" }), "未检测到图形会话");
});

test("empty reason with a command still explains the install step", () => {
  assert.equal(
    dependencyHint({ available: false, install_hint: { command: "apt install ffmpeg" } }),
    "可安装：apt install ffmpeg",
  );
});
