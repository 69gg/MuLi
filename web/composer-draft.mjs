/** One unsubmitted composer snapshot. This is never a send queue. */
export const COMPOSER_DRAFT_KEY = "muli.composer-draft";
export const MAX_DRAFT_ATTACHMENTS = 32;
const attachmentId = /^[a-f0-9]{32}$/;
const encoder = new TextEncoder();

export function composerIdentity(store, main) {
  return typeof store === "string" && attachmentId.test(store) && typeof main === "string" && !!main.trim() && main.length <= 256
    ? { store, main } : null;
}

export function sameComposerIdentity(left, right) {
  return !!left && !!right && left.store === right.store && left.main === right.main;
}

export function validDraftAttachment(file) {
  return !!file && typeof file.id === "string" && attachmentId.test(file.id) && typeof file.name === "string"
    && !!file.name.trim() && !/[\u0000-\u001f]/u.test(file.name) && encoder.encode(file.name).length <= 240;
}

export function createComposerDraft({ storage, maxTextChars }) {
  // JSON can escape each character into six code units. Bound before parsing.
  const maxRecordChars = (maxTextChars + MAX_DRAFT_ATTACHMENTS * 240 + 512) * 6;
  const clear = () => {
    try {
      if (storage().getItem(COMPOSER_DRAFT_KEY) === null) return true;
      storage().removeItem(COMPOSER_DRAFT_KEY);
      return storage().getItem(COMPOSER_DRAFT_KEY) === null;
    } catch { return false; }
  };
  const valid = (record) => record && !Array.isArray(record) && record.schema === 1 && composerIdentity(record.store, record.main)
    && typeof record.text === "string" && record.text.length <= maxTextChars && Array.isArray(record.attachments) && record.attachments.length <= MAX_DRAFT_ATTACHMENTS
    && record.attachments.every(validDraftAttachment) && new Set(record.attachments.map((file) => file.id)).size === record.attachments.length;
  return {
    read(identity) {
      let raw;
      try { raw = storage().getItem(COMPOSER_DRAFT_KEY); } catch { return { record: null, unavailable: true }; }
      if (raw === null) return { record: null };
      let record;
      try { if (typeof raw !== "string" || raw.length > maxRecordChars) throw new Error(); record = JSON.parse(raw); } catch { clear(); return { record: null, invalid: true }; }
      if (!valid(record) || !sameComposerIdentity(record, identity)) { clear(); return { record: null, invalid: true }; }
      return { record: { text: record.text, attachments: record.attachments.map(({ id, name }) => ({ id, name })) } };
    },
    save(identity, text, attachments) {
      const record = { schema: 1, ...identity, text, attachments: attachments.map(({ id, name }) => ({ id, name })) };
      if (!valid(record)) return false;
      if (!text && !attachments.length) return clear();
      try { storage().setItem(COMPOSER_DRAFT_KEY, JSON.stringify(record)); return true; } catch { return false; }
    },
    // A failed invalidation must stop submission before its first side effect.
    consume: clear,
    clear,
  };
}
