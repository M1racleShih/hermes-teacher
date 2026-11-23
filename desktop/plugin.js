import {
  Badge, Button, COMPOSER_AREAS, Dialog, DialogContent, DialogDescription, DialogTitle, Input, Kbd,
  MessageTextContent, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS,
  Textarea, atom, gatewayActivationEpoch, host, useGatewayActivationEpoch, useValue
} from '@hermes/plugin-sdk'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const HEX_ID = /^[a-f0-9]{32}$/
const QUIZ_LABELS = 'ABCDEFGHIJKL'
const POLL_MS = 4000
const SEND_TIMEOUT_MS = 12000

const translator = language => (english, chinese) => language === 'zh' ? chinese : english

function classroomScope() {
  const scope = desktopScope()
  return scope?.runtime && scope?.stored ? scope : null
}

function desktopScope() {
  const runtime = host.state.focusedSessionId.get()
  const stored = host.state.focusedStoredSessionId.get()
  const owner = host.state.focusedSessionOwner.get()
  const connection = host.state.connectionId.get()
  const profile = host.state.profile.get()
  if (!owner || !connection || host.state.gateway.get() !== 'open' ||
      owner.connectionId !== connection || owner.profile !== profile) {
    return null
  }
  const ownerKey = JSON.stringify([connection, profile])
  const gatewayKey = JSON.stringify([ownerKey, gatewayActivationEpoch()])
  return { runtime, stored, profile, connection, ownerKey, gatewayKey, key: JSON.stringify([gatewayKey, stored, runtime]) }
}

async function readInteraction(scope) {
  const result = await host.request('command.dispatch', {
    session_id: scope.runtime,
    name: 'teacher',
    arg: 'interaction-state'
  })
  if (!['exec', 'plugin'].includes(result?.type) || typeof result.output !== 'string') {
    return null
  }
  const { interaction: quiz, language } = JSON.parse(result.output)
  if (!quiz || !['quiz', 'ask'].includes(quiz.kind) || typeof quiz.id !== 'string' || !HEX_ID.test(quiz.id)) {
    return null
  }
  const preference = quiz.kind === 'ask'
  if (!preference && quiz.status === 'feedback' && typeof quiz.feedback === 'string') {
    return { id: quiz.id, kind: quiz.kind, status: 'feedback', feedback: quiz.feedback, language }
  }
  if (quiz.status !== 'open' || !['single', 'multi', ...(preference ? ['text'] : [])].includes(quiz.mode) ||
      typeof quiz.prompt !== 'string' || !Array.isArray(quiz.options) ||
      (quiz.mode === 'text' ? quiz.options.length !== 0 : quiz.options.length < 2) ||
      quiz.options.length > QUIZ_LABELS.length || quiz.options.some((option, index) =>
        !option || typeof option.id !== 'string' || (preference ? option.id !== String(index) : !HEX_ID.test(option.id)) ||
        typeof option.label !== 'string') ||
      new Set(quiz.options.map(option => option.id)).size !== quiz.options.length) {
    return null
  }
  return {
    id: quiz.id,
    kind: quiz.kind,
    status: 'open',
    mode: quiz.mode,
    language,
    intro: typeof quiz.intro === 'string' ? quiz.intro : '',
    prompt: quiz.prompt,
    options: quiz.options,
    hint: typeof quiz.hint === 'string' ? quiz.hint : ''
  }
}

function actionText(quiz, action, selected, note) {
  const t = translator(quiz.language)
  if (action === 'continue') {
    return quiz.status === 'feedback' ? t('continue', '继续') : null
  }
  if (quiz.status !== 'open') {
    return null
  }
  if (action === 'cancel') {
    return t('cancel', '取消')
  }
  if (quiz.kind === 'ask') {
    const answer = note.trim()
    if (quiz.mode === 'text') return answer || null
    const numbers = quiz.options.flatMap((option, index) => selected.includes(option.id) ? [String(index + 1)] : [])
    if (numbers.length !== selected.length || quiz.mode === 'single' && numbers.length + Number(Boolean(answer)) !== 1) {
      return null
    }
    if (answer) numbers.push(t(`other ${answer}`, `自填 ${answer}`))
    return numbers.join(' ') || null
  }
  if (action === 'hint') {
    return quiz.hint ? null : t('hint', '提示')
  }
  const labels = quiz.options.flatMap((option, index) => selected.includes(option.id) ? [QUIZ_LABELS[index]] : [])
  if (action === 'answer' && (!labels.length || labels.length !== selected.length ||
      quiz.mode === 'single' && labels.length !== 1)) {
    return null
  }
  const answer = action === 'unknown' ? t("I don't know", '不知道') : labels.join(' ')
  return note.trim() ? `${answer}\n${note.trim()}` : answer
}

function classroomController(ctx) {
  const snapshot = atom({ key: null, quiz: null, pending: false, error: '' })
  let revision = 0
  let read = null
  let sending = null
  let verifying = false
  let lastBusy = false
  let disposed = false
  let wake = null

  function sameScope(scope) {
    return !disposed && classroomScope()?.key === scope.key
  }

  function update(quiz, error = '') {
    snapshot.set({ ...snapshot.get(), quiz, pending: Boolean(sending || verifying), error })
  }

  async function refresh(scope) {
    if (verifying || read === scope.key) {
      return
    }
    const request = ++revision
    read = scope.key
    try {
      const quiz = await readInteraction(scope)
      if (request !== revision || !sameScope(scope)) {
        return
      }
      const waiting = sending && quiz?.id === sending.id && quiz.status === sending.status &&
        (sending.action !== 'hint' || !quiz.hint)
      const timedOut = waiting && Date.now() >= sending.deadline && !host.state.busy.get()
      if (!waiting || timedOut) {
        sending = null
      }
      const t = translator(quiz?.language)
      update(quiz, timedOut ? t('The classroom has not confirmed this action. Check the latest reply before trying again.', '课堂尚未确认这次操作。请核对最近的回复后重试。') : '')
    } catch {
      // Ordinary profiles may have no Teacher command. Keep their composer quiet.
      if (request === revision && sameScope(scope) && sending && Date.now() >= sending.deadline) {
        sending = null
        const t = translator(snapshot.get().quiz?.language)
        update(snapshot.get().quiz, t('Could not verify the answer. Check the latest classroom reply.', '暂时无法核对作答结果。请查看最近的课堂回复。'))
      }
    } finally {
      if (request === revision) {
        read = null
      }
    }
  }

  function sync(force = false) {
    if (disposed) {
      return
    }
    const scope = classroomScope()
    const key = scope?.key ?? null
    const changed = snapshot.get().key !== key
    if (changed) {
      revision += 1
      read = null
      sending = null
      verifying = false
      snapshot.set({ key, quiz: null, pending: false, error: '' })
    }
    const busy = host.state.busy.get()
    if (scope && !busy && (changed || force || lastBusy)) {
      void refresh(scope)
    }
    lastBusy = busy
  }

  function schedule() {
    if (!wake) {
      wake = ctx.setTimeout(() => {
        wake = null
        sync(true)
      }, 0)
    }
  }

  async function submit(source, action, selected = [], note = '') {
    const scope = classroomScope()
    const current = snapshot.get()
    const t = translator(current.quiz?.language)
    if (!scope || scope.key !== source.key || !current.quiz || current.quiz.id !== source.quiz.id ||
        current.quiz.status !== source.quiz.status || current.pending || host.state.busy.get()) {
      return false
    }
    const request = ++revision
    read = null
    verifying = true
    update(current.quiz)
    let latest
    try {
      latest = await readInteraction(scope)
    } catch {
      if (request === revision && sameScope(scope)) {
        verifying = false
        update(current.quiz, t('Could not verify the classroom question. Try again.', '暂时无法核对课堂问答，请重试。'))
      }
      return false
    }
    if (request !== revision || !sameScope(scope)) {
      return false
    }
    verifying = false
    if (!latest || latest.id !== current.quiz.id || latest.status !== current.quiz.status) {
      update(latest)
      return false
    }
    const text = actionText(latest, action, selected, note)
    if (!text || host.state.busy.get()) {
      update(latest)
      return false
    }
    const marker = translator(latest.language)(`[Classroom answer ${latest.id}]`, `〔课堂作答 ${latest.id}〕`)
    if (!host.composer.submit(scope.stored, `${marker}\n${text}`)) {
      update(latest, t('Open the composer for this lesson and try again.', '请打开这节课的输入区，再试一次。'))
      return false
    }
    sending = { id: latest.id, status: latest.status, action, deadline: Date.now() + SEND_TIMEOUT_MS }
    update(latest)
    return true
  }

  for (const source of [host.state.focusedSessionId, host.state.focusedStoredSessionId,
    host.state.focusedSessionOwner, host.state.connectionId, host.state.profile, host.state.gateway, host.state.busy]) {
    ctx.onDispose(source.listen(schedule))
  }
  ctx.onEvent('message.complete', event => {
    if (event.session_id === classroomScope()?.runtime) {
      schedule()
    }
  })
  ctx.setInterval(() => {
    if (snapshot.get().quiz) {
      sync(true)
    }
  }, POLL_MS)
  ctx.onDispose(() => {
    disposed = true
  })
  schedule()
  return { snapshot, submit, schedule }
}

const cardStyle = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '10px',
  background: 'var(--ui-bg-secondary)',
  color: 'var(--ui-text-primary)',
  minWidth: 0,
  overflow: 'hidden',
  fontSize: '0.875rem'
}
const headerStyle = {
  display: 'flex', alignItems: 'center', gap: '0.65rem', padding: '0.65rem 0.85rem',
  borderBottom: '1px solid var(--ui-stroke-secondary)'
}
const bodyStyle = {
  display: 'grid', gap: '0.65rem', padding: '0.7rem 0.85rem',
  maxHeight: 'min(38vh, 22rem)', overflowY: 'auto', overflowWrap: 'anywhere'
}
const footerStyle = {
  display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.45rem', padding: '0.55rem 0.85rem',
  borderTop: '1px solid var(--ui-stroke-secondary)'
}

function RichText({ text }) {
  return jsx(MessageTextContent, { media: false, previewOnly: true, text })
}

function QuizCard({ model, snapshot }) {
  const { quiz, pending, error } = snapshot
  const t = translator(quiz.language)
  const busy = useValue(host.state.busy)
  const [selected, select] = useState([])
  const [note, setNote] = useState('')
  const card = useRef(null)
  const continueButton = useRef(null)
  const answerFocus = useRef(false)
  const id = useId()
  const awaitingAnswer = quiz.status === 'open'
  const preference = quiz.kind === 'ask'
  const label = preference ? t('Classroom question', '课堂问答') : awaitingAnswer ? t('Classroom quiz', '课堂测验') : t('Answer feedback', '作答解析')
  const noteLabel = preference ? quiz.mode === 'text' ? t('Your response', '填写回答') : t('Other (custom response)', '其他（自填）') : t('Reasoning note (optional)', '推理备注（可选）')
  const disabled = busy || pending

  useEffect(() => {
    if (!awaitingAnswer && !disabled && answerFocus.current) {
      answerFocus.current = false
      if (document.activeElement === document.body || card.current?.contains(document.activeElement)) {
        continueButton.current?.focus({ preventScroll: true })
      }
    }
  }, [awaitingAnswer, disabled])

  async function act(action) {
    if (model.snapshot.get().pending) return
    const answering = action === 'answer' || action === 'unknown'
    if (answering) {
      answerFocus.current = card.current?.contains(document.activeElement) ?? false
    }
    const sent = await model.submit(snapshot, action, selected, note)
    if (!sent && answering) {
      answerFocus.current = false
    }
  }

  return jsxs('section', {
    'aria-busy': disabled,
    'aria-labelledby': `${id}-title`,
    [preference ? 'data-teacher-preference' : 'data-teacher-quiz']: quiz.status,
    ref: card,
    style: cardStyle,
    children: [
      jsxs('header', {
        style: headerStyle,
        children: [
          jsx('strong', { id: `${id}-title`, children: label }),
          awaitingAnswer && quiz.mode !== 'text' ? jsx(Badge, { variant: 'muted', children: quiz.mode === 'single' ? t('Single choice', '单选') : t('Multiple choice', '多选') }) : null
        ]
      }),
      jsx('div', {
        style: bodyStyle,
        ...(awaitingAnswer ? {} : { tabIndex: 0, 'aria-label': t('Feedback text', '解析正文') }),
        children: awaitingAnswer ? [
          quiz.intro ? jsx(RichText, { text: quiz.intro }, 'intro') : null,
          jsx(RichText, { text: quiz.prompt }, 'prompt'),
          quiz.options.length ? jsxs('fieldset', {
            style: { display: 'grid', gap: '0.35rem', minWidth: 0, margin: 0, padding: 0, border: 0 },
            disabled,
            children: [
              jsx('legend', { className: 'sr-only', children: quiz.mode === 'single' ? t('Select one option', '选择一项') : t('Select one or more options', '选择一项或多项') }),
              ...quiz.options.map((option, index) => {
                const checked = selected.includes(option.id)
                return jsxs('label', {
                  style: {
                    display: 'grid', gridTemplateColumns: 'auto 1.7em minmax(0, 1fr)', alignItems: 'start',
                    gap: '0.4rem', padding: '0.4rem 0.6rem', borderRadius: '6px',
                    border: `1px solid var(${checked ? '--ring' : '--ui-stroke-secondary'})`,
                    background: `var(${checked ? '--ui-bg-quaternary' : '--ui-bg-tertiary'})`,
                    cursor: disabled ? 'default' : 'pointer'
                  },
                  children: [
                    jsx('input', {
                      type: quiz.mode === 'single' ? 'radio' : 'checkbox', name: `${id}-answer`, checked,
                      style: { marginTop: '0.2em', accentColor: 'var(--ring)' },
                      onChange: () => {
                        if (quiz.mode === 'single') {
                          select([option.id])
                          if (preference) setNote('')
                        } else {
                          select(previous => previous.includes(option.id) ?
                            previous.filter(key => key !== option.id) : [...previous, option.id])
                        }
                      }
                    }),
                    jsx('span', { style: { color: 'var(--ui-text-secondary)' }, children: `${preference ? index + 1 : QUIZ_LABELS[index]}.` }),
                    jsx(RichText, { text: option.label })
                  ]
                }, option.id)
              })
            ]
          }, 'options') : null,
          quiz.hint ? jsxs('div', {
            role: 'status',
            style: { borderInlineStart: '2px solid var(--ring)', paddingInlineStart: '0.6rem' },
            children: [jsx('strong', { children: t('Hint', '提示') }), jsx(RichText, { text: quiz.hint })]
          }, 'hint') : null,
          jsxs('div', {
            style: { display: 'grid', gap: '0.25rem' },
            children: [
              jsx('label', { htmlFor: `${id}-note`, style: { color: 'var(--ui-text-secondary)' }, children: noteLabel }),
              jsx(Textarea, {
                id: `${id}-note`, rows: 2, style: { minHeight: '3.1rem' },
                value: note, disabled, onChange: event => {
                  const value = event.target.value
                  if (preference && quiz.mode === 'single' && value.trim()) select([])
                  setNote(value)
                }
              })
            ]
          }, 'note')
        ] : jsx(RichText, { text: quiz.feedback })
      }, awaitingAnswer ? 'question' : 'feedback'),
      jsxs('footer', {
        style: footerStyle,
        children: [
          awaitingAnswer ? [
            jsx(Button, { type: 'button', disabled: disabled || !actionText(quiz, 'answer', selected, note), onClick: () => void act('answer'), children: preference ? t('Submit response', '提交回答') : t('Submit answer', '提交答案') }, 'answer'),
            preference || quiz.hint ? null : jsx(Button, { type: 'button', variant: 'outline', disabled, onClick: () => void act('hint'), children: t('Hint', '提示') }, 'hint'),
            preference ? null : jsx(Button, { type: 'button', variant: 'ghost', disabled, onClick: () => void act('unknown'), children: t('I don\'t know', '不知道') }, 'unknown'),
            jsx(Button, { type: 'button', variant: 'ghost', disabled, onClick: () => void act('cancel'), children: preference ? t('Cancel response', '取消回答') : t('Cancel question', '取消本题') }, 'cancel')
          ] : jsxs(Button, {
            type: 'button', ref: continueButton, disabled, onClick: () => void act('continue'),
            children: [t('Continue', '继续'), jsx(Kbd, { 'aria-hidden': true, children: 'Enter' })]
          }),
          pending ? jsx('span', { role: 'status', style: { color: 'var(--ui-text-secondary)' }, children: t('Submitting…', '正在提交…') }) : null,
          error ? jsx('span', { role: 'alert', children: error }) : null
        ]
      })
    ]
  })
}

function QuizPlacement({ model, composers, inComposer }) {
  const count = useValue(composers)
  const snapshot = useValue(model.snapshot)
  const epoch = useGatewayActivationEpoch()

  useEffect(() => model.schedule(), [model, epoch])

  useLayoutEffect(() => {
    if (!inComposer) {
      return
    }
    composers.set(composers.get() + 1)
    return () => composers.set(composers.get() - 1)
  }, [composers, inComposer])

  if (!snapshot.quiz || snapshot.key !== classroomScope()?.key || (count === 1) !== inComposer) {
    return null
  }
  const key = `${snapshot.key}:${snapshot.quiz.id}`
  const card = jsx(QuizCard, { model, snapshot }, key)
  const t = translator(snapshot.quiz.language)
  if (inComposer) {
    return card
  }
  return jsxs(Popover, {
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsx(Button, { type: 'button', variant: 'ghost', size: 'xs', children: snapshot.quiz.kind === 'ask' ? t('Classroom question', '课堂问答') : t('Classroom quiz', '课堂测验') })
      }),
      jsx(PopoverContent, { align: 'end', side: 'top', style: { width: 'min(90vw, 34rem)' }, children: card })
    ]
  }, snapshot.key)
}

const DESKTOP_COMMANDS = new Map([
  ['classroom', 'panel'], ['topic', 'topic'], ['workspace', 'workspace'], ['lesson-end', 'seal'],
  ['lesson-next', 'next'], ['lesson-next-retry', 'next-retry'], ['md-unlog', 'record-off'],
  ['md-log', 'record-bind'], ['lesson-organize', 'organize'], ['role', 'role'], ['subagent', 'role']
])
const NAVIGATION_TEXT = /^(?:继续上次|接着上次|继续最近的课堂|切换主题|选择主题|打开课堂导航|返回学习大厅|(?:切换|换)(?:主题)?(?:到|为)|继续学习|(?:(?:我)?(?:今天|现在|接下来)?(?:我)?想(?:要)?学|今天学|我要学|开始学习)|(?:continue|resume) (?:the )?last lesson\b|open (?:the )?classroom navigator\b|return to (?:the )?classroom lobby\b|switch (?:topics? )?to\b|open (?:a )?topic\b|continue learning\b|(?:today )?(?:i want to|i would like to|i['’]d like to) (?:learn|study)\b)/i

const ROLE_LABELS = {
  researcher: ['Researcher', '研究员'], mermaid: ['Mermaid diagram', 'Mermaid 绘图'], svg: ['SVG diagram', 'SVG 绘图']
}
const ROLE_STATUS = {
  running: ['Running', '进行中'], cancelling: ['Cancelling', '正在取消'], completed: ['Completed', '已完成'],
  cancelled: ['Cancelled', '已取消'], interrupted: ['Interrupted', '已中断'], failed: ['Failed', '失败']
}

function desktopController(ctx) {
  const state = atom({ scope: null, supported: false, language: 'en', open: false, pending: false, message: '', error: '', revision: 0, search: '', section: '' })
  const t = (english, chinese) => translator(state.get().language)(english, chinese)
  let catalog
  let control
  let languageKey
  let disposed = false
  let wake
  const targetWaits = new Set()
  const focusStores = [host.state.focusedSessionId, host.state.focusedStoredSessionId,
    host.state.focusedSessionOwner, host.state.connectionId, host.state.profile, host.state.gateway]

  function update(patch) {
    if (!disposed) state.set({ ...state.get(), ...patch })
  }

  const current = scope => !disposed && desktopScope()?.key === scope.key
  const owner = scope => !disposed && host.state.connectionId.get() === scope.connection &&
    host.state.profile.get() === scope.profile

  async function supported(scope) {
    if (catalog?.key !== scope.gatewayKey) {
      const entry = { key: scope.gatewayKey }
      entry.promise = host.request('commands.catalog', { profile: scope.profile }).then(result => {
        const description = result?.plugin_commands?.['teacher-desktop'] || result?.plugin_commands?.['/teacher-desktop']
        entry.language = /\p{Script=Han}/u.test(description || '') ? 'zh' : 'en'
        return Boolean(description)
      }).catch(() => {
        if (catalog === entry) catalog = null
        return false
      })
      catalog = entry
    }
    return catalog.promise
  }

  async function controllerSession(scope) {
    if (scope.runtime) return scope.runtime
    if (control?.key !== scope.gatewayKey) {
      const entry = { key: scope.gatewayKey }
      const storageKey = `control:${scope.ownerKey}`
      let requestId = ctx.storage.get(storageKey, '')
      if (!requestId) {
        requestId = `teacher-desktop-${crypto.randomUUID()}`
        ctx.storage.set(storageKey, requestId)
      }
      entry.promise = host.request('session.create', {
        profile: scope.profile, hidden: true, toolsets: ['teacher'],
        request_id: requestId
      }).then(result => result?.session_id).catch(error => {
        if (control === entry) control = null
        throw error
      })
      control = entry
    }
    return control.promise
  }

  async function request(scope, action, argument = '') {
    if (!current(scope)) throw new Error(t('The current session changed. Try again.', '当前会话已变化，请重试。'))
    const sessionId = await controllerSession(scope)
    if (!sessionId || !current(scope)) throw new Error(t('The current session changed. Try again.', '当前会话已变化，请重试。'))
    const result = await host.request('command.dispatch', {
      session_id: sessionId, profile: scope.profile, name: 'teacher-desktop',
      arg: JSON.stringify({ action, argument, vault: ctx.storage.get(`vault:${scope.ownerKey}`, '') })
    }, action === 'organize' ? 300000 : undefined)
    if (!['exec', 'plugin'].includes(result?.type) || typeof result.output !== 'string') {
      throw new Error(t('The classroom returned no result for this action.', '课堂操作未返回结果。'))
    }
    const data = JSON.parse(result.output)
    if (current(scope) && ['en', 'zh'].includes(data.language)) {
      languageKey = scope.gatewayKey
      if (state.get().language !== data.language) update({ language: data.language })
    }
    if (data.error) throw new Error(data.error)
    return data
  }

  function schedule() {
    if (wake || disposed) return
    wake = ctx.setTimeout(async () => {
      wake = null
      const scope = desktopScope()
      if (state.get().scope?.key !== scope?.key) {
        const sameOwner = scope?.ownerKey === state.get().scope?.ownerKey
        update({ scope, supported: sameOwner && state.get().supported, open: sameOwner && state.get().open,
          language: sameOwner ? state.get().language : 'en' })
      }
      if (!scope) return
      const enabled = await supported(scope)
      if (current(scope)) update({ supported: enabled,
        language: languageKey === scope.gatewayKey ? state.get().language : catalog?.language || 'en' })
    }, 0)
  }

  function show(search = '', section = '') {
    if (!state.get().supported) return
    update({ open: true, error: '', message: '', search, section })
  }

  function waitForTarget(scope, stored) {
    return new Promise((resolve, reject) => {
      const stops = []
      function finish(target) {
        targetWaits.delete(finish)
        stops.forEach(stop => stop())
        if (target) resolve(target)
        else reject(new Error(t('The target lesson is ready. Open it from the classroom navigator to continue.', '已准备目标课堂。请从课堂导航打开后继续。')))
      }
      function check() {
        const target = desktopScope()
        if (!owner(scope)) finish()
        else if (target && (stored ? target.stored === stored && target.runtime : !target.stored && !target.runtime)) {
          finish(target)
        }
      }
      for (const source of focusStores) stops.push(source.listen(check))
      stops.push(ctx.setTimeout(finish, 5000))
      targetWaits.add(finish)
      check()
    })
  }

  async function transfer(scope, result, oldDraft) {
    if (result.lobby) {
      ctx.storage.set(`vault:${scope.ownerKey}`, result.lobby)
      if (scope.stored) {
        host.newChat(scope.profile)
        await waitForTarget(scope, null)
        if (oldDraft) await host.composer.setDraft('new', oldDraft)
      }
      update({ open: true, revision: state.get().revision + 1 })
      return
    }
    if (!result.session) return
    if (result.session !== scope.stored || !scope.runtime) {
      await host.openSession(result.session, {
        profile: scope.profile, awaitHydration: true, intent: 'in-place', tabTitle: result.topic
      })
    }
    const target = await waitForTarget(scope, result.session)
    update({ open: false })
    const draft = result.draft || oldDraft
    if (draft) {
      const existing = await host.composer.getDraft(target.stored)
      if (!existing && current(target)) await host.composer.setDraft(target.stored, draft)
      else if (existing !== draft) host.notify({ kind: 'warning', title: t('The target lesson already has a draft', '目标课堂已有草稿'), message: draft })
    }
    if (result.kickoff || result.send) {
      if (!current(target)) throw new Error(t('Return to the target lesson to continue.', '请返回目标课堂继续。'))
      await host.request('prompt.submit', {
        profile: target.profile, session_id: target.runtime, finite: true,
        input_id: result.handoff || crypto.randomUUID(), text: result.kickoff || result.send,
        ...(result.kickoff ? { display_kind: 'hidden' } : {})
      })
    }
  }

  async function run(scope, action, argument = '', submitted = '') {
    if (!current(scope) || !await supported(scope) || !current(scope)) return false
    if (state.get().pending || host.state.busy.get()) {
      host.notify({ kind: 'warning', message: t('Wait for the current classroom action to finish.', '请等待当前课堂操作结束。') })
      return false
    }
    update({ pending: true, message: '', error: '' })
    try {
      const address = scope.stored || 'new'
      const opensPanel = action === 'panel' || action === 'workspace-panel'
      const result = opensPanel ? {} : await request(scope, action, argument)
      if (!current(scope)) return false
      const draft = submitted || result.lobby || result.session ? await host.composer.getDraft(address) : null
      const oldDraft = draft === submitted ? '' : draft || ''
      if (!current(scope)) return false
      if (result.pass) {
        if (submitted && (!draft || draft === submitted)) {
          if (draft === submitted) await host.composer.setDraft(address, '')
          if (current(scope)) {
            bypass = { key: scope.key, text: submitted }
            if (!host.composer.submit(address, submitted)) {
              bypass = null
              await host.composer.setDraft(address, submitted)
            }
          }
        }
        return true
      }
      if (submitted && draft === submitted) {
        await host.composer.setDraft(address, '')
      }
      if (!current(scope)) return false
      if (opensPanel) show(argument, action === 'workspace-panel' ? 'workspace' : '')
      await transfer(scope, result, oldDraft)
      if (owner(scope)) {
        const message = result.message || (result.session ? t(`Opened lesson ${result.topic}`, `已打开课堂：${result.topic}`) : '')
        update({ message, revision: state.get().revision + 1 })
        if (message && !state.get().open) host.notify({ kind: 'success', message })
      }
      return result
    } catch (error) {
      if (owner(scope)) {
        const message = t(`Classroom action failed. ${error.message}`, `课堂操作失败。${error.message}`)
        update({ error: message })
        if (!state.get().open) host.notify({ kind: 'error', message })
      }
      return false
    } finally {
      update({ pending: false })
    }
  }

  let bypass = null
  async function middleware(draft) {
    const scope = desktopScope()
    if (!scope || draft.attachments?.length) return draft
    if (bypass?.key === scope.key && bypass?.text === draft.text) {
      bypass = null
      return draft
    }
    const text = draft.text.trim()
    const parts = /^\/([a-z-]+)(?:\s+([\s\S]*))?$/.exec(text)
    let action = parts && DESKTOP_COMMANDS.get(parts[1])
    let argument = parts?.[2] || ''
    if (parts?.[1] === 'teacher' && argument === 'organize') action = 'organize'
    if (action === 'topic' && /^--new(?:\s+|$)/.test(argument)) {
      action = 'new'
      argument = argument.replace(/^--new(?:\s+|$)/, '')
    }
    if (action === 'topic' && !argument.trim()) action = 'panel'
    if (action === 'workspace' && !argument.trim()) action = 'workspace-panel'
    if (parts?.[1] === 'subagent') argument = `start ${argument}`
    if (!action && !text.startsWith('/') && (text && !scope.stored || text !== '开始学习' && NAVIGATION_TEXT.test(text))) {
      action = 'prose'
      argument = text
    }
    if (!action || !await supported(scope) || !current(scope)) return draft
    // The composer restores a cancelled submission first. Clear only that same draft once the
    // command succeeds, and leave it available for correction if the operation fails.
    ctx.setTimeout(() => void run(scope, action, argument, draft.text), 0)
    return null
  }

  for (const source of focusStores) {
    ctx.onDispose(source.listen(schedule))
  }
  ctx.addEventListener(window, 'keydown', event => {
    if (event.key === 'F2' && !event.defaultPrevented && !event.isComposing && !event.repeat &&
        !event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey && state.get().supported) {
      event.preventDefault()
      if (!state.get().open) show()
    }
  })
  ctx.onDispose(() => {
    disposed = true
    targetWaits.forEach(finish => finish())
  })
  schedule()
  const reveal = path => ctx.os.revealPath(path)
  return { state, show, update, request, run, middleware, schedule, reveal }
}

function ClassroomNavigation({ model }) {
  const status = useValue(model.state)
  const t = translator(status.language)
  const epoch = useGatewayActivationEpoch()
  useEffect(() => model.schedule(), [model, epoch])
  if (!status.supported || !status.scope) return null
  return jsxs('div', {
    children: [
      jsx(Button, { type: 'button', variant: 'ghost', size: 'xs', onClick: () => model.show(), children: t('Classroom navigator', '课堂导航') }),
      jsx('output', { 'data-teacher-status': '', 'data-teacher-session': status.scope.stored ?? '',
        className: 'sr-only', children: status.error || status.message }),
      status.open ? jsx(NavigationDialog, { model, status }) : null
    ]
  })
}

const controlsStyle = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.45rem' }

function DirectoryBrowser({ model, scope, language, busy, select, onCancel }) {
  const t = translator(language)
  const [path, setPath] = useState('')
  const [listing, setListing] = useState(null)
  const [error, setError] = useState('')
  const ready = listing?.path === path && !error
  const current = listing?.data

  useEffect(() => {
    let active = true
    model.request(scope, 'browse', path).then(data => {
      if (active) setListing({ path, data })
    }).catch(failure => { if (active) setError(failure.message) })
    return () => { active = false }
  }, [model, scope, path])

  function open(folder) {
    setError('')
    setPath(folder)
  }
  const button = (label, onClick, disabled = false) => jsx(Button, {
    type: 'button', variant: 'outline', size: 'sm', disabled: busy || disabled, onClick, children: label
  })

  return jsxs('section', {
    'aria-label': t('Browse directories', '目录浏览'), 'aria-busy': !ready && !error,
    style: { ...cardStyle, padding: '0.65rem', display: 'grid', gap: '0.5rem' },
    children: [
      jsx('p', { style: { overflowWrap: 'anywhere' }, children: current?.directory || t('Reading directory…', '正在读取目录…') }),
      jsxs('div', { style: controlsStyle, children: [
        button(t('Parent directory', '上一级'), () => open(current.parent), !current?.parent),
        button(t('Select this directory', '选择此目录'), () => void select(current.directory), !ready),
        button(t('Cancel browsing', '取消浏览'), onCancel)
      ] }),
      error ? jsx('p', { role: 'alert', children: error }) : null,
      current?.directories.length ? jsx('ul', { style: { maxHeight: 'min(28vh, 14rem)', overflow: 'auto' }, children:
        current.directories.map(folder => jsx('li', {
          children: jsx(Button, {
            type: 'button', variant: 'ghost', size: 'sm', disabled: busy || !ready,
            onClick: () => open(folder.path), children: folder.name
          })
        }, folder.path))
      }) : ready ? jsx('p', { children: t('This directory has no browsable subdirectories.', '此目录没有可浏览的子目录。') }) : null
    ]
  })
}

function NavigationDialog({ model, status }) {
  const t = translator(status.language)
  const roleLabel = value => ROLE_LABELS[value] ? t(...ROLE_LABELS[value]) : value
  const roleStatus = value => ROLE_STATUS[value] ? t(...ROLE_STATUS[value]) : value
  const [query, setQuery] = useState(status.search)
  const [workspace, setWorkspace] = useState('')
  const [browse, setBrowse] = useState(status.section === 'workspace')
  const [note, setNote] = useState('')
  const [role, setRole] = useState('researcher')
  const [task, setTask] = useState('')
  const [jobId, setJobId] = useState('')
  const [jobs, setJobs] = useState([])
  const [summaryRead, setSummary] = useState(null)
  const [catalogRead, setCatalog] = useState(null)
  const [recentVaults, setRecentVaults] = useState([])
  const busy = useValue(host.state.busy) || status.pending
  const { scope } = status
  const summary = summaryRead?.key === scope.key ? summaryRead.data : null
  const catalog = catalogRead?.key === scope.key ? catalogRead.data : null
  const runningRole = jobs.some(job => job.lesson_id === summary?.lesson?.id && ['running', 'cancelling'].includes(job.status))

  useEffect(() => {
    let active = true
    model.request(scope, 'state').then(data => {
      if (!active) return
      setSummary({ key: scope.key, data })
      if (data.lesson) return model.request(scope, 'roles').then(value => { if (active) setJobs(value.roles) })
      setJobs([])
    }).catch(() => {})
    model.request(scope, 'vaults').then(data => { if (active) setRecentVaults(data.vaults) }).catch(() => {})
    return () => { active = false }
  }, [model, scope, status.revision])

  useEffect(() => {
    if (!runningRole) return
    let active = true
    const timer = setInterval(() => {
      model.request(scope, 'roles').then(data => { if (active) setJobs(data.roles) }).catch(() => {})
    }, 1400)
    return () => { active = false; clearInterval(timer) }
  }, [model, scope, runningRole])

  useEffect(() => {
    setQuery(status.search)
  }, [status.search])

  useEffect(() => {
    if (status.section === 'workspace') setBrowse(true)
  }, [status.section])

  useEffect(() => {
    let active = true
    const timer = setTimeout(() => {
      model.request(scope, 'list', query).then(data => { if (active) setCatalog({ key: scope.key, data }) }).catch(error => {
        if (active) model.update({ error: error.message })
      })
    }, 160)
    return () => { active = false; clearTimeout(timer) }
  }, [model, scope, query, status.revision])

  async function act(action, argument) {
    const result = await model.run(scope, action, argument)
    if (result?.role) {
      setJobId(result.role.id)
      setJobs(previous => [result.role, ...previous.filter(job => job.id !== result.role.id)])
    }
    return result
  }
  const field = (label, value, change) => jsx(Input, {
    'aria-label': label, placeholder: label, value, disabled: busy, onChange: event => change(event.target.value)
  })
  const button = (label, action, argument, disabled = false) => jsx(Button, {
    type: 'button', variant: 'outline', size: 'sm', disabled: busy || disabled,
    onClick: () => void act(action, argument), children: label
  })
  const lesson = summary?.lesson
  const lessonJobs = jobs.filter(item => item.lesson_id === lesson?.id)
  const activeJob = lessonJobs.find(job => job.id === jobId) || lessonJobs[0]
  const image = activeJob?.result?.asset?.image
  return jsx(Dialog, {
    open: true, onOpenChange: opened => { if (!opened && !status.pending) model.update({ open: false }) },
    children: jsxs(DialogContent, {
      style: { maxHeight: '88vh', overflowY: 'auto', width: 'min(95vw, 42rem)' },
      children: [
        jsx(DialogTitle, { children: t('Classroom navigator', '课堂导航') }),
        jsx(DialogDescription, { children: catalog?.vault || summary?.vault || t('Loading library…', '正在读取资料库') }),
        jsxs('section', { style: { display: 'grid', gap: '0.45rem' }, children: [
          field(t('Search or enter a topic', '搜索或输入主题'), query, setQuery),
          jsxs('div', { style: controlsStyle, children: [
            button(t('Open topic', '打开主题'), 'topic', query.trim(), !query.trim()),
            button(t('New lesson on this topic', '同主题新课'), 'new', query.trim() || lesson?.topic, !query.trim() && !lesson),
            button(t('Resume last lesson', '继续上次'), 'resume'),
            button(t('Temporary discussion', '临时讨论'), 'temporary'),
            lesson ? button(t('Return to the lobby', '返回学习大厅'), 'lobby') : null
          ] }),
          catalog?.lessons.length ? jsx('ul', { style: { maxHeight: '15vh', overflow: 'auto' }, children:
            catalog.lessons.map(row => jsx('li', { children: jsx(Button, {
              type: 'button', variant: 'ghost', disabled: busy,
              onClick: () => void act('open', row.id),
              children: `${row.topic} · ${row.id.slice(0, 8)}${row.preview ? ` · ${row.preview.slice(0, 80)}` : ''}`
            }) }, row.id))
          }) : catalog ? jsx('p', { children: t('No matching lessons.', '没有符合条件的课堂。') }) : null
        ] }),
        jsxs('details', { open: status.section === 'workspace' || undefined, children: [
          jsx('summary', { children: t('Library', '学习资料库') }),
          browse ? jsx(DirectoryBrowser, {
            model, scope, busy, language: status.language,
            onCancel: () => setBrowse(false),
            select: async path => { if (await act('workspace', path)) setBrowse(false) }
          }, scope.key) : jsx(Button, {
            type: 'button', variant: 'outline', size: 'sm', disabled: busy,
            onClick: () => setBrowse(true), children: t('Browse directories', '浏览目录')
          }),
          field(t('Library path', '资料库路径'), workspace, setWorkspace),
          jsxs('div', { style: controlsStyle, children: [
            button(t('Switch library', '切换资料库'), 'workspace', workspace.trim(), !workspace.trim()),
            button(t('Create and switch', '创建并切换'), 'workspace-create', workspace.trim(), !workspace.trim()),
            ...recentVaults.filter(path => path !== summary?.vault).map(path => jsx('div', {
              children: button(path, 'workspace', path)
            }, path))
          ] })
        ] }),
        lesson ? jsxs('details', { children: [
          jsx('summary', { children: t('Recording and handoff', '记录与续课') }),
          jsx('p', { children: lesson.recording ? lesson.record || t('Recording this lesson', '正在记录课堂') : t('Lesson recording is off', '课堂记录已停止') }),
          jsxs('div', { style: controlsStyle, children: [
            lesson.recording ? button(t('Stop recording', '停录'), 'record-off') : button(t('Resume recording', '恢复记录'), 'record-on'),
            button(t('Archive notes', '封存笔记'), 'seal'), button(t('Archive and organize', '封存并整理'), 'organize'),
            button(t('View handoff notes', '查看续课说明'), 'checkpoint'), button(t('Continue in a new session', '带说明换会话'), 'next'),
            lesson.handoff ? button(t('Check handoff request', '核对续课请求'), 'next-retry') : null
          ] }),
          field(t('Existing Markdown path', '现有 Markdown 路径'), note, setNote),
          button(t('Link notes and backfill', '绑定并回填笔记'), 'record-bind', note.trim(), !note.trim())
        ] }) : null,
        lesson ? jsxs('details', { children: [
          jsx('summary', { children: t('Classroom collaborators', '课堂协作者') }),
          jsxs('select', { 'aria-label': t('Classroom collaborators', '课堂协作者'), value: role, disabled: busy, onChange: event => setRole(event.target.value), children: [
            ...Object.keys(ROLE_LABELS).map(value => jsx('option', { value, children: roleLabel(value) }, value))
          ] }),
          field(t('Task or follow-up instructions', '任务内容或补充说明'), task, setTask),
          lessonJobs.length ? jsx('select', {
            'aria-label': t('Collaborator tasks', '已启动的角色任务'), value: activeJob?.id ?? '', disabled: busy,
            onChange: event => setJobId(event.target.value),
            children: lessonJobs.map(item => jsx('option', {
              value: item.id, children: `${roleLabel(item.role)} · ${roleStatus(item.status)} · ${item.id.slice(0, 8)}`
            }, item.id))
          }) : null,
          jsxs('div', { style: controlsStyle, children: [
            button(t('Start collaborator', '启动角色'), 'role', `start ${role} ${task}`, !task.trim()),
            activeJob ? button(t('View status', '查看状态'), 'role', `status ${activeJob.id}`) : null,
            activeJob ? button(t('Add instructions', '补充任务'), 'role', `supplement ${activeJob.id} ${task}`, !task.trim()) : null,
            activeJob ? button(t('Cancel task', '取消任务'), 'role', `cancel ${activeJob.id}`) : null,
            activeJob ? button(t('Resume task', '恢复任务'), 'role', `resume ${activeJob.id}`) : null
          ] }),
          activeJob ? jsx('p', { children: `${roleLabel(activeJob.role)} · ${roleStatus(activeJob.status)} · ${activeJob.id}` +
            (activeJob.revision ? t(` · Revision ${activeJob.revision}`, ` · 第 ${activeJob.revision} 版`) : '') }) : null,
          activeJob?.result?.text || activeJob?.result?.summary ? jsx(RichText, {
            text: activeJob.result.text || activeJob.result.summary
          }) : null,
          image ? jsx(Button, {
            type: 'button', variant: 'outline', size: 'sm', children: t('Show diagram in file manager', '在文件管理器查看图示'),
            onClick: () => void model.reveal(`${summary.vault}/${image}`)
          }) : null,
          activeJob?.error ? jsx('p', { role: 'alert', children: activeJob.error }) : null
        ] }) : null,
        status.pending ? jsx('p', { role: 'status', children: t('Working…', '正在处理课堂操作…') }) : null,
        status.error ? jsx('p', { role: 'alert', children: status.error }) : null,
        status.message ? jsx('div', { role: 'status', children: jsx(RichText, { text: status.message }) }) : null
      ]
    })
  })
}

export default {
  id: 'hermes-teacher',
  name: 'Hermes Teacher',
  register(ctx) {
    const model = classroomController(ctx)
    const navigation = desktopController(ctx)
    const composers = atom(0)
    ctx.registerMany([
      { id: 'quiz', area: COMPOSER_AREAS.top, render: () => jsx(QuizPlacement, { model, composers, inComposer: true }) },
      { id: 'quiz-status', area: STATUSBAR_AREAS.right, render: () => jsx(QuizPlacement, { model, composers, inComposer: false }) },
      { id: 'navigation', area: STATUSBAR_AREAS.left, render: () => jsx(ClassroomNavigation, { model: navigation }) },
      { id: 'classroom-input', area: COMPOSER_AREAS.middleware, data: { handler: navigation.middleware } }
    ])
  }
}
