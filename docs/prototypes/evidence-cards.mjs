// Presentation only. No decisions, reviews, or source objects are changed here.
// A page is a reading group, NOT an assertion of shared applicability.
export const ISSUE_STATUSES = new Set(['error', 'manual', 'warning', 'suspect', 'mismatch', 'unresolved']);
const key = value => JSON.stringify(value);
const counts = values => values.reduce((out, value) => { out[value ?? 'unknown'] = (out[value ?? 'unknown'] || 0) + 1; return out; }, {});
const statusRank = status => ({error: 0, mismatch: 0, manual: 1, unresolved: 1, suspect: 1, unknown: 1, warning: 2, pass: 3, info: 4}[status] ?? 4);
const boxOK = box => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) && box[0] < box[2] && box[1] < box[3];
const own = (value, field) => value != null && Object.hasOwn(value, field);
const objectOK = value => value != null && typeof value === 'object' && !Array.isArray(value);
const textID = value => typeof value === 'string' && value.length > 0 && value === value.trim();
const ROLES = new Set(['report', 'ptr', 'record_9706_1', 'record_9706_202']);
const HASH_FIELDS = ['document_sha256', 'blob_sha256', 'sha256'];
const canonicalHash = value => /^[a-f\d]{64}$/i.test(value) ? value.toLowerCase() : value;
export function locationKey(location) {
  return key([location.document_key, location.pdf_page, location.bbox || null]);
}

export function bindEvidenceLocation(loc, documents, context, label = '', expectedRole = null) {
  const fail = reason => ({location: null, reason, raw: loc});
  if (!objectOK(loc) || !objectOK(documents)) return fail('invalid_location_or_documents');
  if (['role', 'document_role', 'document_id', ...HASH_FIELDS].some(field => field in loc && !own(loc, field))) return fail('inherited_evidence_identity');
  const roles = ['role', 'document_role'].filter(field => own(loc, field)).map(field => loc[field]);
  if (expectedRole !== null) roles.push(expectedRole);
  if (!roles.length || roles.some(role => typeof role !== 'string' || !ROLES.has(role))) return fail('invalid_role');
  if (roles.some(role => role !== roles[0])) return fail('role_conflict');
  const role = roles[0];
  if (!own(documents, role) || !objectOK(documents[role])) return fail('unbound_role');
  const doc = documents[role];
  if (['document_id', 'id', ...HASH_FIELDS].some(field => field in doc && !own(doc, field))) return fail('inherited_current_document_identity');
  const ids = ['document_id', 'id'].filter(field => own(doc, field)).map(field => doc[field]);
  if (!ids.length || ids.some(value => !textID(value))) return fail('missing_or_invalid_document_id');
  if (ids.some(value => value !== ids[0])) return fail('conflicting_current_document_ids');
  const documentId = ids[0];
  const hashes = HASH_FIELDS.filter(field => own(doc, field)).map(field => doc[field]);
  if (hashes.some(value => !textID(value))) return fail('invalid_current_document_hash');
  if (hashes.some(value => canonicalHash(value) !== canonicalHash(hashes[0]))) return fail('conflicting_current_document_hashes');
  const hash = hashes.length ? canonicalHash(hashes[0]) : null;
  if (own(loc, 'document_id') && (!textID(loc.document_id) || loc.document_id !== documentId)) return fail('evidence_document_id_mismatch');
  const claimedHashes = HASH_FIELDS.filter(field => own(loc, field)).map(field => loc[field]);
  if (claimedHashes.some(value => !textID(value))) return fail('invalid_evidence_document_hash');
  if (claimedHashes.length && !hash) return fail('evidence_document_hash_unverifiable');
  if (claimedHashes.some(value => canonicalHash(value) !== hash)) return fail('evidence_document_hash_mismatch');
  const page = Number(loc.pdf_page ?? loc.page);
  if (!Number.isInteger(page) || page < 1 || typeof (loc.pdf_page ?? loc.page) === 'boolean') return fail('invalid_page');
  if (doc.page_count && page > doc.page_count) return fail('page_out_of_range');
  const box = loc.bbox || loc.rect;
  return {location: { role, document_key: key([context, role, documentId, hash]),
    document_id: documentId, ...(hash ? {document_sha256: hash} : {}), filename: doc.original_filename || doc.filename || doc.name || role,
    pdf_page: page, bbox: boxOK(box) ? [...box] : null, label: label || loc.semantic_role || loc.source || '证据',
    semantic_role: label || loc.semantic_role || loc.source || '证据', raw: loc }, reason: null};
}

export function locatedEvidence(loc, documents, context, label = '') {
  return bindEvidenceLocation(loc, documents, context, label).location;
}

export function catalogFromFindings(findings, { context, documents, rowsFor, titleFor }) {
  const entries = [];
  const rawFindings = [];
  findings.forEach((finding, index) => {
    const findingKey = key([context, index, finding.id ?? finding.rule_id ?? null]);
    const common = { finding_key: findingKey, original_finding_id: finding.id ?? null,
      rule_id: finding.rule_id ?? null, finding_status: finding.status ?? finding.machine_status,
      title: titleFor(finding), finding_index: index };
    rawFindings.push({ finding_key: findingKey, raw: finding });
    const rows = rowsFor(finding);
    const seen = new Set();
    rows.forEach((row, rowIndex) => {
      const unlocated = [];
      const locations = ['a', 'b'].map(which => {
        const side = row[which];
        if (side?.unlocated_evidence) unlocated.push({which, ...side.unlocated_evidence});
        if (!side?.loc) return null;
        const bound = bindEvidenceLocation(side.loc, documents, context, side.title);
        if (!bound.location) unlocated.push({which, reason: bound.reason, raw: side.loc});
        return bound.location ? { ...bound.location, which } : null;
      }).filter(Boolean);
      locations.forEach(loc => seen.add(locationKey(loc)));
      entries.push({ ...common, entry_id: key([findingKey, 'row', rowIndex]), kind: 'check_row', row_index: rowIndex,
        status: row.status ?? 'unknown', label: row.label || common.title, project: row.sub || '',
        condition: key([finding.rule_id || finding.id, row.label, row.sub, row.a?.text, row.b?.text, row.note]),
        text: [row.a?.text, row.b?.text, row.note, row.problem].filter(Boolean).join('\n'),
        locations, unlocated_evidence: unlocated, raw: row });
    });
    // Keep official evidence not represented by the existing detail-table rows.
    // The original finding/IDs and complete evidence arrays are also retained.
    const unlocated = [];
    const extra = [...(Array.isArray(finding.evidence) ? finding.evidence : []), ...(finding.evidence_locations || [])]
      .map(loc => {
        const bound = bindEvidenceLocation(loc, documents, context);
        if (!bound.location) unlocated.push({reason: bound.reason, raw: loc});
        return bound.location;
      }).filter(Boolean).filter(loc => {
        const id = locationKey(loc); if (seen.has(id)) return false; seen.add(id); return true;
      });
    if (!rows.length || extra.length || unlocated.length) entries.push({ ...common, entry_id: key([findingKey, 'finding']), kind: 'finding_evidence',
      status: common.finding_status, label: common.title, project: '', condition: key([findingKey, 'unprojected_evidence']),
      text: finding.summary || '', locations: extra, unlocated_evidence: unlocated, raw: { finding_key: findingKey } });
  });
  return { schema: 'evidence-card-catalog-v1', context, documents, raw_findings: rawFindings, entries };
}

export function groupEvidence(catalog) {
  const cards = new Map();
  let locationReferences = 0;
  for (const entry of catalog.entries) {
    const locations = [...entry.locations, ...(!entry.locations.length || entry.unlocated_evidence?.length ? [null] : [])];
    for (const loc of locations) {
      if (loc) locationReferences += 1;
      // Different documents/roles/runs never collapse because page numbers match.
      const pageKey = loc ? key([loc.document_key, loc.pdf_page]) : key(['unlocated', entry.finding_key]);
      if (!cards.has(pageKey)) cards.set(pageKey, { card_id: pageKey, location: loc, groups: new Map(), entry_ids: new Set() });
      const card = cards.get(pageKey);
      // Exact occurrence + condition only. Text equality across pages/scopes is not identity.
      const groupKey = key([loc ? locationKey(loc) : entry.entry_id, entry.condition]);
      if (!card.groups.has(groupKey)) card.groups.set(groupKey, { group_id: groupKey, location: loc, entries: [], reference_count: 0 });
      const group = card.groups.get(groupKey);
      group.reference_count += 1;
      if (!group.entries.some(item => item.entry_id === entry.entry_id)) group.entries.push(entry);
      card.entry_ids.add(entry.entry_id);
    }
  }
  const byId = new Map(catalog.entries.map(e => [e.entry_id, e]));
  const result = [...cards.values()].map(card => {
    const ids = [...card.entry_ids];
    const groups = [...card.groups.values()].map(g => ({...g, entries: [...g.entries].sort((a,b) => statusRank(a.status)-statusRank(b.status))}));
    groups.sort((a,b) => statusRank(a.entries[0].status)-statusRank(b.entries[0].status));
    // The primary location and entry must travel together AFTER sorting.
    // Grouping by physical occurrence does not establish a shared left/right pane.
    const primaryEntry = groups[0].entries[0];
    const primaryLocation = entryLocation(primaryEntry, groups[0]);
    return { ...card, location: primaryLocation, groups, entry_ids: ids,
      status_counts: counts(ids.map(id => byId.get(id).status)),
      finding_status_counts: counts([...new Map(ids.map(id => { const e = byId.get(id); return [e.finding_key, e.finding_status]; })).values()]) };
  });
  result.sort((a, b) => {
    const rank = card => 'error' in card.status_counts || 'error' in card.finding_status_counts ? 0
      : Object.keys(card.status_counts).some(s => ISSUE_STATUSES.has(s)) || Object.keys(card.finding_status_counts).some(s => ISSUE_STATUSES.has(s)) ? 1 : 2;
    return rank(a) - rank(b) || String(a.location?.document_key || '~').localeCompare(String(b.location?.document_key || '~'))
      || (a.location?.pdf_page || 0) - (b.location?.pdf_page || 0);
  });
  const linkedPages = result.filter(c => c.location).length;
  const detailLocations = catalog.entries.filter(e => e.kind === 'check_row').flatMap(e => e.locations);
  const detailPages = new Set(detailLocations.map(loc => key([loc.document_key, loc.pdf_page]))).size;
  return { cards: result, metrics: { original_findings: catalog.raw_findings.length, entries: catalog.entries.length,
    entry_status_counts: counts(catalog.entries.map(e => e.status)),
    finding_status_counts: counts(catalog.raw_findings.map(f => f.raw.status ?? f.raw.machine_status)),
    display_cards: result.length, linked_page_cards: linkedPages, unlocated_cards: result.length - linkedPages,
    exact_occurrence_condition_groups: result.reduce((sum, c) => sum + c.groups.length, 0),
    location_references: locationReferences,
    detail_location_references: detailLocations.length,
    distinct_detail_pages: detailPages,
    potential_repeated_detail_page_openings: detailLocations.length - detailPages,
    potential_repeated_page_openings: locationReferences - linkedPages,
    metric_scope: 'Reading each displayed location reference once versus opening each unique document page once. Detail expansion, comparisons and actual user clicks/time are not measured.' } };
}

export function entryLocation(entry, group) {
  if (!group.location) return null;
  const wanted = locationKey(group.location);
  return entry.locations.find(loc => locationKey(loc) === wanted) || null;
}

export function renderEvidenceCards(container, catalog, { onOpen, onInspect }) {
  const grouped = groupEvidence(catalog);
  container.replaceChildren();
  const labels = { error: '问题', manual: '待确认', warning: '警示', pass: '一致', info: '信息', unknown: '状态未知' };
  const format = stat => Object.entries(stat).map(([s, n]) => `${labels[s] || s} ${n}`).join(' · ');
  grouped.cards.forEach(card => {
    const li = document.createElement('li'); li.className = 'evidence-card'; li.dataset.cardId = card.card_id;
    const top = document.createElement('div'); top.className = 'evidence-card-head';
    const title = document.createElement('button'); title.type = 'button'; title.className = 'evidence-page-open';
    title.textContent = card.location ? `${card.location.filename} · PDF 第 ${card.location.pdf_page} 页` : '未定位证据 · 保留待核';
    title.disabled = !card.location;
    title.addEventListener('click', () => onOpen(entryLocation(card.groups[0].entries[0], card.groups[0]), card.groups[0].entries[0])); top.append(title);
    const stats = document.createElement('p'); stats.className = 'evidence-card-status';
    stats.textContent = `明细：${format(card.status_counts)}；所属核对结果：${format(card.finding_status_counts)}`;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = `查看 ${card.entry_ids.length} 条关联明细 · ${card.groups.length} 个来源/条件组`;
    details.append(summary);
    details.addEventListener('toggle', () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = 'true';
      card.groups.forEach(group => {
        const block = document.createElement('section'); block.className = 'evidence-condition';
        const body = document.createElement('p'); body.textContent = group.entries[0].text || '原始结果未提供文字说明'; block.append(body);
        let members = block;
        if (group.entries.length > 1) {
          members = document.createElement('details'); members.className = 'evidence-shared-members';
          const shared = document.createElement('summary');
          shared.textContent = `同一来源/条件关联 ${group.entries.length} 项：${format(counts(group.entries.map(e => e.status)))}；展开查看全部项目和 ID`;
          members.append(shared); block.append(members);
        }
        group.entries.forEach(entry => {
          const line = document.createElement('div'); line.className = 'evidence-member'; line.dataset.entryId = entry.entry_id;
          const button = document.createElement('button'); button.type = 'button'; button.className = 'evidence-entry-open';
          button.textContent = `${labels[entry.status] || entry.status} · ${entry.label}${entry.project ? ' · ' + entry.project : ''}`;
          button.addEventListener('click', () => onInspect(entry, entryLocation(entry, group)));
          const id = document.createElement('small');
          id.textContent = `原始 ID：${entry.original_finding_id ?? '未提供'} · ${entry.row_index === undefined ? entry.kind : '明细 ' + (entry.row_index + 1)} · 所属结果 ${labels[entry.finding_status] || entry.finding_status}`;
          line.append(button, id);
          if (!group.location && entry.unlocated_evidence?.length) {
            const issue = document.createElement('p');
            const reasons = new Set(entry.unlocated_evidence.map(item => item.reason));
            const names = {invalid_role:'来源角色无效', role_conflict:'来源角色互相冲突', unbound_role:'未绑定该角色的文档',
              inherited_evidence_identity:'证据身份字段不是自身记录', inherited_current_document_identity:'当前文档身份字段不是自身记录',
              missing_or_invalid_document_id:'当前文档缺少有效 ID', conflicting_current_document_ids:'当前文档 ID 互相冲突',
              invalid_current_document_hash:'当前文档哈希无效', conflicting_current_document_hashes:'当前文档哈希互相冲突',
              evidence_document_id_mismatch:'证据文档 ID 与当前文档不一致', invalid_evidence_document_hash:'证据文档哈希无效',
              evidence_document_hash_unverifiable:'缺少当前文档哈希，无法核对证据身份', evidence_document_hash_mismatch:'证据文档哈希与当前文档不一致',
              invalid_location_or_documents:'证据或文档信息格式无效', invalid_bbox:'证据坐标无效',
              invalid_page:'证据页码无效', page_out_of_range:'证据页码超出当前文档范围'};
            issue.className = 'evidence-binding-warning';
            issue.textContent = '证据未定位：' + [...reasons].map(reason => names[reason] || reason).join('；') + '。原始证据已保留，核对状态未变。';
            line.append(issue);
          }
          const raw = document.createElement('details'); const rawTitle = document.createElement('summary'); rawTitle.textContent = '查看完整原始明细与证据';
          const pre = document.createElement('pre'); raw.append(rawTitle, pre);
          raw.addEventListener('toggle', () => { if (raw.open && !pre.textContent) pre.textContent = JSON.stringify({ entry, finding: catalog.raw_findings.find(f => f.finding_key === entry.finding_key)?.raw }, null, 2); });
          line.append(raw); members.append(line);
        });
        details.append(block);
      });
    });
    li.append(top, stats, details); container.append(li);
  });
  return grouped.metrics;
}
