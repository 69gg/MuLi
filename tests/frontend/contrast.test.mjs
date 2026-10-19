import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../../web/styles.css", import.meta.url), "utf8");
const luminance = (hex) => {
  const [r, g, b] = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255).map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
  return r * .2126 + g * .7152 + b * .0722;
};
const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);

test("light and dark body, hints, placeholders and button text retain WCAG AA contrast", () => {
  for (const theme of [":root", ':root[data-theme="dark"]']) {
    const block = css.slice(css.indexOf(`${theme} {`)).split("}", 1)[0];
    const tokens = Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[\da-f]{6});/gi)].map(([, key, value]) => [key, value]));
    for (const foreground of ["text", "text-strong", "muted", "faint", "accent", "accent-dark", "danger", "success"]) {
      for (const background of ["canvas", "surface", "surface-2", "sidebar", "selected"]) {
        assert.ok(contrast(tokens[foreground], tokens[background]) >= 4.5, `${theme}: ${foreground} on ${background} must be at least 4.5:1`);
      }
    }
    assert.ok(contrast(tokens["on-accent"], tokens.accent) >= 4.5);
    assert.ok(contrast(tokens["on-accent"], tokens["accent-dark"]) >= 4.5);
  }
  for (const selector of [".composer textarea::placeholder", ".secondary-button", ".row-content p", ".field small", ".composer-footnote"]) {
    const rule = css.slice(css.indexOf(`${selector} {`)).split("}", 1)[0];
    assert.match(rule, /color:\s*var\(--muted\)/, `${selector} should use the readable text token`);
  }
});
