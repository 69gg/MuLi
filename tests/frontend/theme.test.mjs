import test from "node:test";
import assert from "node:assert/strict";
import { resolveTheme } from "../../web/theme.mjs";

test("an explicit theme wins over the OS and invalid preferences follow the OS", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
  assert.equal(resolveTheme(null, true), "dark");
  assert.equal(resolveTheme("unknown", false), "light");
});
