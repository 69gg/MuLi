import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../../web/styles.css", import.meta.url), "utf8");
const tokens = Object.fromEntries([...css.matchAll(/--([\w-]+):\s*(#[\da-f]{6});/gi)].map(([, key, value]) => [key, value]));
const luminance = (hex) => {
  const [r, g, b] = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255).map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
  return r * .2126 + g * .7152 + b * .0722;
};
const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);

test("body, hints, placeholders and button text retain WCAG AA contrast", () => {
  for (const foreground of ["text", "text-strong", "muted", "faint", "accent", "accent-dark"]) {
    for (const background of ["canvas", "surface", "sidebar"]) {
      assert.ok(contrast(tokens[foreground], tokens[background]) >= 4.5, `${foreground} on ${background} must be at least 4.5:1`);
    }
  }
  for (const foreground of ["danger", "success"]) assert.ok(contrast(tokens[foreground], tokens.canvas) >= 4.5);
  assert.ok(contrast("#f2f8fa", tokens.accent) >= 4.5);
  assert.ok(contrast(tokens.muted, "#e6f0f4") >= 4.5);
  assert.ok(contrast(tokens["accent-dark"], "#aac9d5") >= 4.5);
  for (const selector of [".composer textarea::placeholder", ".secondary-button", ".row-content p", ".field small", ".composer-footnote"]) {
    const rule = css.slice(css.indexOf(`${selector} {`)).split("}", 1)[0];
    assert.match(rule, /color:\s*var\(--muted\)/, `${selector} should use the readable text token`);
  }
});
