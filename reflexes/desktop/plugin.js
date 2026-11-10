// Reflexes: the Hermes Desktop half. A page to arm and manage reflexes, and a status-bar chip.
// Plain ESM, loaded uncompiled: no JSX, and only these three import specifiers resolve.
import {
  host, useValue, atom, useQuery, queryClient,
  ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS,
  Button, Input, Textarea, Switch
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'reflexes'
const ROUTE = '/reflexes'
const STATE_KEY = [ID, 'state']

let api = null // the plugin context, set in register(); gives ctx.rest and ctx.files
const $pulse = atom(0) // bumped when a reflex fires, so the chip can flash

const muted = { color: 'var(--ui-text-tertiary)' }
const secondary = { color: 'var(--ui-text-secondary)' }
const card = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: 12 }
const row = { display: 'flex', alignItems: 'center', gap: 8 }
const label = { ...muted, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }
const mono = { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: 12, overflowWrap: 'anywhere' }

const refresh = () => queryClient.invalidateQueries({ queryKey: STATE_KEY })
const errorText = e => String((e && (e.detail || e.message)) || e || 'Something went wrong')

function ago(ts, now) {
  if (!ts) return 'never'
  const s = Math.max(0, Math.round(now - ts))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function sessionLabel(session, now, focusedId) {
  const tail = session.key.split(/[:-]/).pop() || session.key
  const here = focusedId && session.key.includes(focusedId) ? ' (the chat you have open)' : ''
  return `${session.surface || 'chat'} ${tail.slice(0, 8)}, active ${ago(session.last_seen, now)}${here}`
}

function useReflexState() {
  return useQuery({ queryKey: STATE_KEY, queryFn: () => api.rest('/state'), refetchInterval: 5000 })
}

async function call(path, body) {
  try {
    await api.rest(path, { method: 'POST', body: body || {} })
    refresh()
    return true
  } catch (e) {
    host.notify({ kind: 'error', message: errorText(e) })
    return false
  }
}

function Field({ title, children }) {
  return jsxs('div', { style: { display: 'flex', flexDirection: 'column', minWidth: 0 }, children: [
    jsx('div', { style: label, children: title }), children
  ] })
}

const KINDS = [
  { id: 'folder', title: 'Folder', hint: 'a file appears in a folder' },
  { id: 'drive', title: 'Drive', hint: 'a USB stick, SD card or external disk is connected' },
  { id: 'log', title: 'Log file', hint: 'new lines matching some text appear in a file' }
]
const KIND_TITLE = { folder: 'Folder', drive: 'Drive', log: 'Log file' }
const DEFAULT_PATTERN = { folder: '*', drive: '', log: '' }

const EXAMPLES = [
  { title: 'Receipts', kind: 'folder', pattern: '*.pdf, *.png, *.jpg',
    instruction: 'Each new file is a receipt. Rename it to date_vendor_amount, move it to Documents/Receipts/<year>, and add a row to receipts.csv. Reply in one line.' },
  { title: 'Screenshots', kind: 'folder', pattern: 'Screenshot*.png, Screen Shot*.png',
    instruction: 'Each new file is a screenshot. Name it after what it shows and move it to Pictures/Screenshots.' },
  { title: 'Camera card', kind: 'drive', pattern: '',
    instruction: "Copy the new photos and videos from the drive into Pictures/Imports/<today's date>. Never delete anything from the drive. Report how many files you copied." },
  { title: 'Build errors', kind: 'log', pattern: 'error',
    instruction: 'Read the new log lines and find the cause in the project. Propose a fix, but do not change any files without asking me first.' }
]

function AddForm({ data }) {
  const focusedId = useValue(host.state.focusedStoredSessionId)
  const [kind, setKind] = useState('folder')
  const [folder, setFolder] = useState('')
  const [file, setFile] = useState('')
  const [patterns, setPatterns] = useState(DEFAULT_PATTERN)
  const [instruction, setInstruction] = useState('')
  const [picked, setPicked] = useState('')
  const [busy, setBusy] = useState(false)
  const sessions = data.sessions || []
  const fallback = (sessions.find(s => focusedId && s.key.includes(focusedId)) || sessions[0] || {}).key || ''
  const target = picked && sessions.some(s => s.key === picked) ? picked : fallback
  const pattern = patterns[kind]
  const setPattern = v => setPatterns({ ...patterns, [kind]: v })
  const needs = kind === 'folder' ? folder.trim() : kind === 'log' ? file.trim() && pattern.trim() : true
  const ready = needs && instruction.trim() && target && !busy

  const browse = async () => {
    try {
      const res = await api.files.requestFolder()
      if (res && res.ok && res.folder && res.folder.path) setFolder(res.folder.path)
    } catch (e) {
      host.notify({ kind: 'error', message: errorText(e) })
    }
  }
  const applyExample = ex => {
    setKind(ex.kind)
    setPatterns({ ...patterns, [ex.kind]: ex.pattern })
    setInstruction(ex.instruction)
  }
  const submit = async () => {
    setBusy(true)
    const ok = await call('/rules', {
      kind, folder: kind === 'folder' ? folder : '', file: kind === 'log' ? file : '',
      pattern, instruction, session_key: target
    })
    setBusy(false)
    if (ok) {
      setFolder(''); setFile(''); setInstruction('')
      host.notify({ kind: 'success', message: kind === 'folder' ? 'Reflex armed. Files already in the folder are ignored.'
        : kind === 'drive' ? 'Reflex armed. It fires when a drive is connected from now on.'
        : 'Reflex armed. Only lines added from now on are checked.' })
    }
  }

  const current = KINDS.find(k => k.id === kind)
  let fields
  if (kind === 'folder') {
    fields = jsxs('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 1fr)', gap: 10 }, children: [
      jsx(Field, { title: 'When a file appears in', children: jsxs('div', { style: row, children: [
        jsx(Input, { value: folder, placeholder: 'Folder path', onChange: e => setFolder(e.target.value), style: { flex: 1, minWidth: 0 } }),
        jsx(Button, { variant: 'outline', size: 'sm', onClick: browse, children: 'Browse' })
      ] }) }),
      jsx(Field, { title: 'Matching', children:
        jsx(Input, { value: pattern, placeholder: '*.pdf, *.png', onChange: e => setPattern(e.target.value) }) })
    ] })
  } else if (kind === 'drive') {
    fields = jsx(Field, { title: 'Drive name (optional)', children: jsxs('div', { children: [
      jsx(Input, { value: pattern, placeholder: 'e.g. CAMERA, or leave empty', onChange: e => setPattern(e.target.value) }),
      jsx('div', { style: { ...muted, fontSize: 12, marginTop: 4 }, children: 'Leave empty to react to any drive. Otherwise only drives whose name or path matches are used.' })
    ] }) })
  } else {
    fields = jsxs('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 2fr)', gap: 10 }, children: [
      jsx(Field, { title: 'When new lines appear in', children:
        jsx(Input, { value: file, placeholder: 'Full path of the log file', onChange: e => setFile(e.target.value) }) }),
      jsx(Field, { title: 'Text to look for', children: jsxs('div', { children: [
        jsx(Input, { value: pattern, placeholder: 'error', onChange: e => setPattern(e.target.value) }),
        jsx('div', { style: { ...muted, fontSize: 11, marginTop: 4 }, children: 'Plain text, or /pattern/ for a regular expression.' })
      ] }) })
    ] })
  }

  return jsxs('div', { style: { ...card, display: 'flex', flexDirection: 'column', gap: 10 }, children: [
    jsx('div', { style: { fontWeight: 600 }, children: 'New reflex' }),
    jsxs('div', { style: { ...row, flexWrap: 'wrap' }, children: [
      jsx('span', { style: { ...muted, fontSize: 12 }, children: 'Start from an example:' }),
      ...EXAMPLES.map(ex => jsx(Button, { key: ex.title, variant: 'outline', size: 'sm', onClick: () => applyExample(ex), children: ex.title }))
    ] }),
    jsxs('div', { children: [
      jsx('div', { style: label, children: 'React when' }),
      jsx('div', { style: { ...row, flexWrap: 'wrap' }, children: KINDS.map(k =>
        jsx(Button, { key: k.id, variant: k.id === kind ? 'default' : 'outline', size: 'sm', 'aria-pressed': k.id === kind,
          onClick: () => setKind(k.id), children: k.title })) }),
      jsx('div', { style: { ...muted, fontSize: 12, marginTop: 4 }, children: `Starts when ${current.hint}.` })
    ] }),
    fields,
    jsx(Field, { title: 'Hermes should', children:
      jsx(Textarea, { value: instruction, rows: 3, onChange: e => setInstruction(e.target.value),
        placeholder: kind === 'folder' ? 'Write the whole instruction: what the files are, what to do with each, where results go.'
          : kind === 'drive' ? 'Write the whole instruction: what to look for on the drive and what to do with it.'
          : 'Write the whole instruction: what the matching lines mean and what to do about them.' }) }),
    jsxs('div', { style: { ...row, justifyContent: 'space-between', flexWrap: 'wrap' }, children: [
      jsx(Field, { title: 'In this chat', children: sessions.length
        ? jsx('select', {
            value: target, onChange: e => setPicked(e.target.value),
            style: { background: 'transparent', color: 'inherit', border: '1px solid var(--ui-stroke-secondary)',
              borderRadius: 6, padding: '4px 8px', fontSize: 12, maxWidth: 420 },
            children: sessions.map(s => jsx('option', { value: s.key, children: sessionLabel(s, data.now, focusedId) }, s.key))
          })
        : jsx('div', { style: { ...muted, fontSize: 12 }, children: 'No chats seen yet. Send any message in a chat, then come back.' }) }),
      jsx(Button, { size: 'sm', disabled: !ready, onClick: submit, children: busy ? 'Arming…' : 'Arm reflex' })
    ] })
  ] })
}

function RuleCard({ rule, now }) {
  const [confirming, setConfirming] = useState(false)
  const base = `/rules/${encodeURIComponent(rule.name)}`
  const kind = rule.kind || 'folder'
  return jsxs('div', { style: { ...card, display: 'flex', flexDirection: 'column', gap: 6, opacity: rule.enabled ? 1 : 0.65 }, children: [
    jsxs('div', { style: { ...row, justifyContent: 'space-between' }, children: [
      jsxs('div', { style: row, children: [
        jsx(Switch, { checked: rule.enabled, onCheckedChange: on => call(`${base}/${on ? 'resume' : 'pause'}`) }),
        jsx('span', { style: { fontWeight: 600 }, children: rule.name }),
        jsx('span', { style: { ...muted, fontSize: 12 }, children: rule.enabled ? 'armed' : 'paused' })
      ] }),
      jsxs('div', { style: row, children: [
        jsx(Button, { variant: 'ghost', size: 'sm', onClick: () => call(`${base}/test`).then(ok => ok &&
          host.notify({ kind: 'info', message: `Test firing sent to the chat for '${rule.name}'.` })), children: 'Test' }),
        confirming
          ? jsx(Button, { variant: 'destructive', size: 'sm', onClick: () => call(`${base}/remove`), children: 'Really remove' })
          : jsx(Button, { variant: 'ghost', size: 'sm', onClick: () => setConfirming(true), children: 'Remove' })
      ] })
    ] }),
    jsxs('div', { style: { ...secondary, fontSize: 12 }, children: [
      jsx('span', { style: { ...muted, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 4, padding: '0 5px', marginRight: 6, fontSize: 11 },
        children: KIND_TITLE[kind] || kind }),
      rule.target
        ? jsx('span', { style: mono, children: rule.target })
        : jsxs('span', { children: [`${(rule.events || []).join(' / ')} files matching `, jsx('span', { style: mono, children: rule.pattern }), ' in ',
            jsx('span', { style: mono, children: rule.folder }), rule.recursive ? ' and subfolders' : ''] })
    ] }),
    jsx('div', { style: { fontSize: 13, whiteSpace: 'pre-wrap' }, children: rule.instruction }),
    jsx('div', { style: { ...muted, fontSize: 12 }, children:
      `Fired ${rule.fire_count} time${rule.fire_count === 1 ? '' : 's'}, last ${ago(rule.last_fired_at, now)}. Delivers to ${rule.surface || 'its chat'}.` }),
    rule.problem && rule.problem !== 'paused by you' ? jsx('div', { style: { color: 'var(--ui-accent)', fontSize: 12 }, children: `Problem: ${rule.problem}` }) : null
  ] })
}

const STATUS_TEXT = { delivered: 'delivered', pending: 'waiting', delivering: 'delivering', missed: 'missed', suppressed: 'stopped' }

function Firings({ events, now }) {
  if (!events.length) return jsx('div', { style: { ...muted, fontSize: 12 }, children: 'Nothing has fired yet.' })
  return jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 }, children: events.map(e =>
    jsxs('div', { style: { ...row, fontSize: 12, alignItems: 'baseline' }, children: [
      jsx('span', { style: { ...muted, width: 64, flexShrink: 0 }, children: ago(e.created_at, now) }),
      jsx('span', { style: { fontWeight: 600, flexShrink: 0 }, children: e.rule }),
      jsx('span', { style: { ...secondary, flex: 1, minWidth: 0 }, children: e.detail ? `${e.summary} (${e.detail})` : e.summary }),
      jsx('span', { style: { ...(e.status === 'delivered' ? muted : { color: 'var(--ui-accent)' }), flexShrink: 0 }, children: STATUS_TEXT[e.status] || e.status })
    ] }, e.id)) })
}

function ReflexesPage() {
  const { data, isLoading, error } = useReflexState()
  let body
  if (isLoading) body = jsx('div', { style: muted, children: 'Loading…' })
  else if (error || !data) {
    body = jsxs('div', { style: { ...card, ...secondary, fontSize: 13 }, children: [
      jsx('div', { style: { fontWeight: 600 }, children: 'The Reflexes backend is not answering.' }),
      jsx('div', { children: 'Enable the plugin for this profile with: hermes plugins enable reflexes' }),
      jsx('div', { style: { ...muted, fontSize: 12 }, children: errorText(error) })
    ] })
  } else {
    body = jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 16 }, children: [
      data.problem ? jsx('div', { style: { ...card, borderColor: 'var(--ui-accent)', fontSize: 13 }, children: data.problem }) : null,
      !data.detecting ? jsx('div', { style: { ...card, fontSize: 13, ...secondary }, children:
        'Nothing is watching right now. Reflexes run while the Hermes gateway is running; it may still be starting.' }) : null,
      jsx(AddForm, { data }),
      jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 }, children: [
        jsx('div', { style: label, children: `Reflexes (${data.rules.length})` }),
        data.rules.length
          ? data.rules.map(r => jsx(RuleCard, { rule: r, now: data.now }, r.id))
          : jsx('div', { style: { ...muted, fontSize: 13 }, children: 'None yet. Arm one above, type /reflex add in a chat, or just ask Hermes to watch something for you.' })
      ] }),
      jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 }, children: [
        jsx('div', { style: label, children: 'Recent firings' }),
        jsx(Firings, { events: data.events, now: data.now })
      ] })
    ] })
  }
  return jsx('div', { style: { height: '100%', overflowY: 'auto' }, children:
    jsxs('div', { style: { maxWidth: 820, margin: '0 auto', padding: 24, display: 'flex', flexDirection: 'column', gap: 16 }, children: [
      jsxs('div', { children: [
        jsx('div', { style: { fontSize: 20, fontWeight: 600 }, children: 'Reflexes' }),
        jsx('div', { style: { ...secondary, fontSize: 13 }, children: 'Hermes starts a turn by itself when something happens on this computer: a file appears, a drive is connected, or a log line shows up.' })
      ] }),
      body
    ] }) })
}

function Chip() {
  const { data } = useReflexState()
  const pulse = useValue($pulse)
  const armed = data && data.rules ? data.rules.filter(r => r.enabled).length : 0
  const flashing = pulse > 0 && Date.now() - pulse < 4000
  return jsx('button', {
    type: 'button', title: 'Reflexes', onClick: () => host.navigate(ROUTE),
    style: { padding: '0 6px', fontSize: 11, color: flashing || (data && data.problem) ? 'var(--ui-accent)' : 'var(--ui-text-tertiary)' },
    children: `⚡ ${armed}`
  })
}

export default {
  id: ID,
  name: 'Reflexes',
  register(ctx) {
    api = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(ReflexesPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Reflexes', codicon: 'zap' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(Chip, {}) }
    ])
    ctx.onEvent(`plugin.${ID}.fired`, event => {
      const p = (event && event.payload) || {}
      $pulse.set(Date.now())
      ctx.setTimeout(() => $pulse.set(0), 4200)
      refresh()
      host.notify({ kind: 'info', message: `Reflex '${p.rule || '?'}' fired: ${p.summary || 'event'}` })
    })
    ctx.onEvent(`plugin.${ID}.delivered`, refresh)
    ctx.onEvent(`plugin.${ID}.paused`, event => {
      const p = (event && event.payload) || {}
      refresh()
      host.notify({ kind: 'warning', message: `Reflex '${p.rule || '?'}' ${p.reason || 'paused itself'}` })
    })
    ctx.onDispose(() => { api = null })
  }
}
