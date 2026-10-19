import test from "node:test";
import assert from "node:assert/strict";
import { attachmentKind, attachmentURL, formatBytes } from "../../web/attachments.mjs";

test("local media embeds exclude active documents and external attachment URLs", () => {
  assert.equal(attachmentKind({ mime_type: "image/png" }), "image");
  assert.equal(attachmentKind({ mime_type: "video/webm" }), "video");
  assert.equal(attachmentKind({ mime_type: "audio/ogg" }), "audio");
  for (const mime_type of ["image/svg+xml", "text/html", "application/pdf", "image/unknown", ""]) assert.equal(attachmentKind({ mime_type }), "file");
  const file = { id: "unsafe/path?value#part", url: "https://external.example/file", preview_url: "javascript:alert(1)" };
  assert.equal(attachmentURL(file), "/api/uploads/unsafe%2Fpath%3Fvalue%23part/content");
  assert.equal(attachmentURL(file, true), "/api/uploads/unsafe%2Fpath%3Fvalue%23part/download");
  assert.equal(attachmentURL({}), null);
});

test("attachment sizes remain readable without losing sub-kilobyte values", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(1023), "1023 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(32 * 1024 * 1024), "32 MB");
  assert.equal(formatBytes(-1), "");
  assert.equal(formatBytes(Infinity), "");
});
