import { execFile } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { delimiter, join } from 'node:path'

const OPTION_LETTERS = 'ABCDEFGHIJKL'
const defaultLanguage = process.env.HERMES_TEACHER_LANGUAGE?.split(/[-_]/, 1)[0].toLowerCase() === 'zh' ? 'zh' : 'en'
const translator = language => (english, chinese) => language === 'zh' ? chinese : english
const switchedSession = language => translator(language)(
  'The active session changed. Open the classroom panel again.', '已切换会话。请重新打开课堂面板。'
)

function activeSession() {
  try {
    return JSON.parse(readFileSync(process.env.HERMES_TUI_ACTIVE_SESSION_FILE, 'utf8')).session_id
  } catch {
    return null
  }
}

function classroomRequest(request, language) {
  const t = translator(language)
  const { HERMES_HOME: profile, HERMES_PYTHON: python, HERMES_PYTHON_SRC_ROOT: source } = process.env
  if (!profile || !python || !source) {
    return Promise.reject(new Error(t('Open the classroom in Hermes Ink.', '请用 Hermes Ink 打开课堂。')))
  }
  const env = { ...process.env, PYTHONPATH: [join(profile, 'plugins/hermes-teacher'), source].join(delimiter) }
  return new Promise((resolve, reject) => {
    const child = execFile(python, ['-m', 'teacher.quiz_widget'], { env, timeout: 300_000 }, (error, stdout) => {
      if (error) {
        reject(new Error(t('Could not fetch the classroom state. Press R to refresh.', '无法获取课堂状态。按 R 刷新后可继续。')))
        return
      }
      try {
        const result = JSON.parse(stdout)
        if (result.error) {
          reject(Object.assign(new Error(result.error), { language: result.language }))
        } else {
          resolve(result)
        }
      } catch {
        reject(new Error(t('Could not read the classroom state.', '无法读取课堂状态。')))
      }
    })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify(request))
  })
}

function fitLines(text, width) {
  const lines = []
  for (const paragraph of text.split('\n')) {
    let line = ''
    let cells = 0
    for (const char of paragraph) {
      const size = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Extended_Pictographic}\u3000-\u303f\uff01-\uff60]/u.test(char) ? 2 : 1
      if (cells + size > width) {
        lines.push(line)
        line = ''
        cells = 0
      }
      line += char
      cells += size
    }
    lines.push(line)
  }
  return lines
}

export default function register(sdk) {
  const { Dialog, Overlay, Text, defineWidgetApp, h, isCtrl, updateWidget } = sdk
  const labels = translator(defaultLanguage)

  function run(state, action) {
    const payload = {
      action,
      session_id: state.session,
      question_id: action === 'read' ? undefined : state.card?.id,
      ...(action === 'answer' ? { options: state.selected } : {}),
      ...(['answer', 'unknown'].includes(action) ? { note: state.note } : {})
    }
    const identity = JSON.stringify(payload)
    const requestId = state.retry?.identity === identity ? state.retry.requestId : randomUUID()
    const apply = fn => updateWidget(app, current => {
      if (current.instance !== state.instance) return current
      if (activeSession() !== state.session) {
        return { ...current, pending: false, card: null, message: switchedSession(current.language) }
      }
      return fn(current)
    })

    void classroomRequest({ ...payload, request_id: requestId }, state.language).then(result => apply(current => {
      const card = result.card
      const sameQuestion = current.card?.id === card?.id && card?.status === 'open'
      const language = result.language || current.language
      const t = translator(language)
      const message = !result.classroom
        ? t('The active session is not linked to a lesson.', '当前会话没有关联课堂。')
        : card ? '' : result.message ?? t('This lesson has no questions to answer.', '当前课堂没有待回答的问题。')
      return {
        ...current,
        card,
        cursor: sameQuestion ? current.cursor : -1,
        editing: false,
        language,
        message,
        note: sameQuestion ? current.note : '',
        pending: false,
        retry: null,
        scroll: 0,
        selected: sameQuestion ? current.selected : []
      }
    }), error => apply(current => ({
      ...current,
      language: error.language || current.language,
      message: error.message,
      pending: false,
      retry: { identity, requestId },
      scroll: 0
    })))

    return { ...state, pending: true, message: '', editing: false }
  }

  function select(state, index) {
    const id = state.card.options[index].id
    const selected = state.card.mode === 'single'
      ? [id]
      : state.selected.includes(id) ? state.selected.filter(value => value !== id) : [...state.selected, id]
    const note = state.card.kind === 'ask' && state.card.mode === 'single' ? '' : state.note
    return { ...state, cursor: index, selected, note }
  }

  const app = defineWidgetApp({
    id: 'teacher-quiz',
    help: labels('Open questions and quizzes for this lesson.', '打开当前课堂的题目或偏好问询'),
    usage: labels('Usage /teacher-quiz', '用法 /teacher-quiz'),

    init(arg) {
      if (arg.trim()) return null
      const state = {
        card: null,
        cursor: -1,
        editing: false,
        instance: randomUUID(),
        language: defaultLanguage,
        message: '',
        note: '',
        pending: false,
        retry: null,
        scroll: 0,
        selected: [],
        session: activeSession()
      }
      return state.session ? run(state, 'read') : { ...state, message: labels(
        'The active session is not ready. Open the classroom panel again.', '当前会话还未就绪。请重新打开课堂面板。'
      ) }
    },

    reduce(state, { ch, key }) {
      if (isCtrl(key, ch, 'c') || (key.escape && !state.editing)) return null
      if (state.session !== activeSession()) {
        return { ...state, pending: false, card: null, message: switchedSession(state.language) }
      }
      if (key.pageUp || key.pageDown) {
        const delta = key.pageDown ? 4 : -4
        return { ...state, scroll: Math.max(0, state.scroll + delta) }
      }
      if (state.pending) return state
      if (state.editing) {
        if (key.escape || key.tab || (key.return && !key.shift)) return { ...state, editing: false }
        if (key.backspace || key.delete) return { ...state, note: [...state.note].slice(0, -1).join('') }
        const input = (key.return && key.shift) || isCtrl(key, ch, 'j') ? '\n' : !key.ctrl && !key.meta ? ch : ''
        const note = (state.note + input.replace(/[\u0000-\u0009\u000b-\u001f\u007f]/gu, '')).slice(0, 4096)
        const selected = state.card.kind === 'ask' && state.card.mode === 'single' && note.trim() ? [] : state.selected
        return { ...state, note, selected }
      }
      const shortcut = ch.toUpperCase()
      if (shortcut === 'R' && state.session) {
        return { ...run(state, 'read'), retry: state.retry }
      }
      if (!state.card) return key.return ? null : state
      if (state.card.status === 'feedback') return key.return ? run(state, 'continue') : state

      if (key.tab) return { ...state, editing: true }
      const preference = state.card.kind === 'ask'
      const count = state.card.options.length
      if (count && (key.upArrow || key.downArrow)) {
        const delta = key.downArrow ? 1 : -1
        const cursor = state.cursor < 0 ? (key.downArrow ? 0 : count - 1) : (state.cursor + count + delta) % count
        return { ...state, cursor }
      }
      if (ch === ' ' && state.cursor >= 0) return select(state, state.cursor)
      const choice = /^[1-9]$/.test(ch) ? Number(ch) - 1
        : !preference && shortcut.length === 1 ? OPTION_LETTERS.indexOf(shortcut) : -1
      if (choice >= 0 && choice < count) return select(state, choice)
      if (key.return) {
        if (state.selected.length || (preference && state.note.trim())) return run(state, 'answer')
        if (state.cursor >= 0 && state.card.mode === 'single') return run(select(state, state.cursor), 'answer')
        if (preference) return { ...state, editing: true }
        return { ...state, message: translator(state.language)('Select an option first.', '请先选择选项。'), scroll: 0 }
      }
      const action = preference ? (shortcut === 'X' && 'cancel') : { '?': 'hint', U: 'unknown', X: 'cancel' }[shortcut]
      return action ? run(state, action) : state
    },

    render({ cols, rows, state, t }) {
      const width = Math.max(18, Math.min(cols - 2, 88))
      const card = state.card
      const tr = translator(state.language)
      const feedback = card?.status === 'feedback'
      const preference = card?.kind === 'ask'
      const field = preference ? card.mode === 'text' ? tr('Response', '回答') : tr('Other', '其他（自填）') : tr('Note', '备注')
      const title = feedback ? tr('Quiz feedback', '测验反馈') : preference
        ? tr('Classroom question', '课堂问询') : tr('Classroom quiz', '课堂测验')
      const instructions = state.editing
        ? tr(`${field} · Shift+Enter for a new line · Enter to keep`, `填写${field} · Shift+Enter 换行 · Enter 保留`)
        : feedback
          ? tr('Enter to continue · R to refresh · Esc to return to chat', 'Enter 继续 · R 刷新 · Esc 返回对话')
          : preference
            ? (card.mode === 'text'
                ? tr('Tab to write a response · Enter to submit', 'Tab 写回答 · Enter 提交')
                : tr('↑↓ to move · 1–9 or Space to select · Tab for Other · Enter to submit', '↑↓ 移动 · 1–9 或空格选取 · Tab 自填 · Enter 提交')
              ) + tr('\nX to cancel · R to refresh · Esc to return to chat', '\nX 取消 · R 刷新 · Esc 返回对话')
            : card
              ? tr(
                  '↑↓ to move · Letters, 1–9, or Space to select · Enter to submit · Tab for a note\n' +
                  '? for a hint · U for I don’t know · X to cancel · R to refresh · Esc to return to chat',
                  '↑↓ 移动 · 字母、1–9 或空格选取 · Enter 提交 · Tab 备注\n? 提示 · U 不知道 · X 取消题目 · R 刷新 · Esc 返回对话'
                )
              : tr('Enter or Esc to return to chat · R to refresh', 'Enter 或 Esc 返回对话 · R 刷新')
      const blocks = []
      if (feedback) {
        blocks.push(card.feedback)
      } else if (card) {
        if (card.intro) blocks.push(card.intro, '')
        blocks.push(card.prompt)
        if (card.mode !== 'text') blocks.push(card.mode === 'multi' ? tr('Select one or more', '可选择多项') : tr('Select one', '选择一项'))
        const indent = row => `${row === state.cursor ? '▸' : ' '} `
        card.options.forEach((option, index) => {
          const mark = state.selected.includes(option.id) ? '●' : '○'
          blocks.push(`${indent(index)}${mark} ${preference ? index + 1 : OPTION_LETTERS[index]}. ${option.label}`)
        })
        if (card.hint) blocks.push('', tr(`Hint ${card.hint}`, `提示 ${card.hint}`))
        if (state.editing || state.note) blocks.push('', `${field} ${state.note}${state.editing ? '▏' : ''}`)
      }
      if (state.pending) blocks.push(feedback ? tr('Continuing the lesson…', '正在继续课堂…') : tr('Loading lesson…', '正在载入课堂…'))
      if (state.message) blocks.push(state.message)

      const lines = fitLines(blocks.join('\n'), width - 6)
      const footer = fitLines(instructions, width - 6)
      const available = Math.max(3, rows - footer.length - 9)
      const offset = Math.min(state.scroll, Math.max(0, lines.length - available))
      const more = lines.length > available ? tr('PgUp/PgDn to scroll\n', 'PgUp/PgDn 滚动\n') : ''
      return h(Overlay, { zone: 'center' }, h(Dialog, { title, width, hint: more + instructions },
        ...lines.slice(offset, offset + available).map((line, index) =>
          h(Text, { key: index, color: t.color.text, wrap: 'truncate-end' }, line || ' ')
        )
      ))
    }
  })
}
