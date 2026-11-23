import assert from 'node:assert/strict'
import { createRequire, registerHooks } from 'node:module'
import { join } from 'node:path'
import { after, test } from 'node:test'
import { pathToFileURL } from 'node:url'

// Run against the React and DOM-test dependencies of an existing Desktop checkout.
const desktop = process.env.HERMES_DESKTOP_SOURCE
if (!desktop) {
  throw new Error('Set HERMES_DESKTOP_SOURCE to the Hermes apps/desktop directory with node_modules.')
}
const require = createRequire(join(desktop, 'package.json'))
const { JSDOM } = require('jsdom')
const dom = new JSDOM('<body><main></main></body>', { pretendToBeVisual: true })
Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true })
const { act, Fragment, useSyncExternalStore } = require('react')
const { createRoot } = require('react-dom/client')
const { jsx } = require('react/jsx-runtime')
const { fireEvent, within } = require('@testing-library/dom')

function atom(initial) {
  let current = initial
  const listeners = new Set()
  const listen = listener => {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }
  return {
    get: () => current,
    set(next) {
      if (next !== current) {
        current = next
        for (const listener of listeners) listener(next)
      }
    },
    listen
  }
}

const epoch = atom(0)
const state = Object.fromEntries(Object.entries({
  focusedSessionId: 'runtime-a', focusedStoredSessionId: 'lesson-a',
  focusedSessionOwner: { connectionId: 'local', profile: 'teacher' },
  connectionId: 'local', profile: 'teacher', gateway: 'open', busy: false
}).map(([name, initial]) => [name, atom(initial)]))
const useValue = store => useSyncExternalStore(store.listen, store.get)
const SDK = {
  atom, useValue, gatewayActivationEpoch: epoch.get, useGatewayActivationEpoch: () => useValue(epoch),
  host: { state, composer: {} },
  COMPOSER_AREAS: { top: 'composer.top', middleware: 'composer.middleware' },
  STATUSBAR_AREAS: { left: 'statusBar.left', right: 'statusBar.right' },
  Badge: ({ children }) => jsx('span', { children }),
  Button: ({ variant, size, ...props }) => jsx('button', props),
  Kbd: props => jsx('kbd', props),
  MessageTextContent: ({ text }) => jsx('div', { children: text }),
  Textarea: props => jsx('textarea', props),
  Input: props => jsx('input', props),
  Dialog: ({ children }) => jsx(Fragment, { children }),
  DialogContent: ({ children }) => jsx('section', { children }),
  DialogDescription: ({ children }) => jsx('p', { children }),
  DialogTitle: ({ children }) => jsx('h2', { children }),
  Popover: ({ children }) => jsx(Fragment, { children }),
  PopoverTrigger: ({ children }) => children,
  PopoverContent: ({ children }) => jsx('div', { children })
}
globalThis.teacherQuizTestSDK = SDK
const sdkURL = `data:text/javascript,${encodeURIComponent(`export const {${Object.keys(SDK).join(',')}} = globalThis.teacherQuizTestSDK`)}`
const reactURLs = new Map(['react', 'react/jsx-runtime'].map(specifier => [specifier, pathToFileURL(require.resolve(specifier)).href]))
const imports = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@hermes/plugin-sdk') return { shortCircuit: true, url: sdkURL }
    if (reactURLs.has(specifier)) return { shortCircuit: true, url: reactURLs.get(specifier) }
    return nextResolve(specifier, context)
  }
})
const { default: plugin } = await import('../../desktop/plugin.js')

after(() => {
  imports.deregister()
  dom.window.close()
  delete globalThis.teacherQuizTestSDK
})

const ids = { question: 'a'.repeat(32), next: 'b'.repeat(32), a: '1'.repeat(32), b: '2'.repeat(32) }
const open = (mode = 'single', id = ids.question) => ({
  id, status: 'open', mode, prompt: '选择正确的分母。', intro: '先收窄样本空间。',
  options: [{ id: ids.a, label: '条件事件的概率' }, { id: ids.b, label: '联合事件的概率' }]
})
const feedback = { id: ids.question, status: 'feedback', feedback: '答错。\n\n分母取条件事件的概率。' }
const reply = (quiz, type = 'exec', language = 'zh') => ({ type, output: JSON.stringify({
  language,
  interaction: quiz ? { kind: 'quiz', ...quiz } : null
}) })

async function surface(t, { quiz = open(), split = false, language = 'zh', navLanguage } = {}) {
  for (const [key, value] of Object.entries({
    focusedSessionId: 'runtime-a', focusedStoredSessionId: 'lesson-a',
    focusedSessionOwner: { connectionId: 'local', profile: 'teacher' },
    connectionId: 'local', profile: 'teacher', gateway: 'open', busy: false
  })) state[key].set(value)
  const main = document.querySelector('main')
  const root = createRoot(main)
  const contributions = new Map()
  const events = new Map()
  const timers = new Set()
  const polls = new Set()
  const subscriptions = []
  const reads = []
  const sends = []
  const controls = { quiz, readOverride: null, composerAccepts: true }
  let middleware

  SDK.host.request = async (method, params) => {
    if (method === 'commands.catalog') {
      const description = navLanguage === 'zh' ? '桌面课堂的导航和操作' : 'Navigate and manage lessons from Desktop'
      return { plugin_commands: navLanguage ? { 'teacher-desktop': description } : {} }
    }
    reads.push([method, params])
    if (params.name === 'teacher-desktop') {
      const { action } = JSON.parse(params.arg)
      return { type: 'plugin', output: JSON.stringify({
        language, vault: '/test-library', lessons: [], vaults: [],
        ...(action === 'prose' ? { pass: true } : {})
      }) }
    }
    const override = controls.readOverride
    controls.readOverride = null
    return override ? override() : reply(params.session_id === 'runtime-a' ? controls.quiz : null, 'exec', language)
  }
  SDK.host.composer.submit = (session, text) => {
    sends.push([session, text])
    return controls.composerAccepts
  }
  SDK.host.composer.getDraft = () => ''
  plugin.register({
    registerMany: items => items.forEach(item => {
      contributions.set(item.area, item.render)
      if (item.data?.handler) middleware = item.data.handler
    }),
    storage: { get: (_, fallback) => fallback },
    onDispose: fn => subscriptions.push(fn),
    addEventListener() {},
    onEvent: (type, fn) => events.set(type, fn),
    setTimeout(fn) { timers.add(fn); return () => timers.delete(fn) },
    setInterval(fn) { polls.add(fn); return () => polls.delete(fn) }
  })
  const render = area => contributions.get(area)()
  await act(() => root.render(jsx(Fragment, {
    children: [
      jsx('div', { id: 'composer', children: render('composer.top') }, 'main'),
      split ? jsx('div', { id: 'second-composer', children: render('composer.top') }, 'split') : null,
      jsx('div', { id: 'status', children: render('statusBar.right') }, 'status'),
      jsx('div', { id: 'navigator', children: render('statusBar.left') }, 'navigator'),
      jsx('textarea', { 'aria-label': '聊天输入', defaultValue: '稍后我要问的问题。' }, 'draft')
    ]
  })))
  async function settle(operation = () => {}) {
    await act(operation)
    await act(() => {
      for (const timer of timers) {
        timers.delete(timer)
        timer()
      }
    })
  }
  async function publish(next) {
    controls.quiz = next
    await settle(() => events.get('message.complete')({ session_id: 'runtime-a' }))
  }
  t.after(async () => {
    await act(() => root.unmount())
    for (const dispose of subscriptions) dispose()
  })
  await settle()
  return { main, view: within(main), reads, sends, controls, publish, settle, polls, middleware }
}

test('single choice and note reach the exact lesson, then feedback and continuation', async t => {
  const { view, reads, sends, publish, settle } = await surface(t)
  const first = view.getByRole('radio', { name: /^A\.\s*条件事件的概率$/ })
  const second = view.getByRole('radio', { name: /^B\.\s*联合事件的概率$/ })
  const draft = view.getByRole('textbox', { name: '聊天输入' })
  assert.deepEqual(reads[0], ['command.dispatch', { session_id: 'runtime-a', name: 'teacher', arg: 'interaction-state' }])
  assert.ok(!view.queryByText('答错。'), '作答前应隐藏解析')
  assert.equal(view.getByText('单选').textContent, '单选')
  assert.equal(view.getByRole('button', { name: '提交答案' }).disabled, true)
  await settle(() => {
    fireEvent.click(first)
    fireEvent.click(second)
    fireEvent.change(view.getByRole('textbox', { name: '推理备注（可选）' }), { target: { value: '我的推导\n第二行' } })
  })
  assert.equal(first.checked, false)
  assert.equal(second.checked, true)
  assert.equal(sends.length, 0)
  await settle(() => {
    const submit = view.getByRole('button', { name: '提交答案' })
    submit.focus()
    fireEvent.click(submit)
    fireEvent.click(submit)
  })
  assert.deepEqual(sends, [['lesson-a', `〔课堂作答 ${ids.question}〕\nB\n我的推导\n第二行`]])
  assert.equal(draft.value, '稍后我要问的问题。')
  assert.equal(view.getByRole('button', { name: '提交答案' }).disabled, true)

  await publish(feedback)
  assert.match(view.getByRole('region', { name: '作答解析' }).textContent, /答错/)
  assert.ok(document.activeElement === view.getByRole('button', { name: '继续' }), '作答后焦点应移到继续按钮')
  await settle(() => fireEvent.click(view.getByRole('button', { name: '继续' })))
  assert.deepEqual(sends[1], ['lesson-a', `〔课堂作答 ${ids.question}〕\n继续`])
  await publish(open('single', ids.next))
  assert.ok(!view.queryByRole('button', { name: '继续' }), '新题应收起上一题的继续按钮')
  assert.equal(view.getByRole('button', { name: '提交答案' }).disabled, true)
})

test('English controls preserve question text and submit English actions', async t => {
  const { view, sends, publish, settle } = await surface(t, { language: 'en' })
  const card = within(view.getByRole('region', { name: 'Classroom quiz' }))
  assert.equal(card.getByText('选择正确的分母。').textContent, '选择正确的分母。')
  await settle(() => fireEvent.click(card.getByRole('button', { name: "I don't know" })))
  assert.deepEqual(sends[0], ['lesson-a', `[Classroom answer ${ids.question}]\nI don't know`])
  await publish(feedback)
  await settle(() => fireEvent.click(view.getByRole('button', { name: 'Continue' })))
  assert.deepEqual(sends[1], ['lesson-a', `[Classroom answer ${ids.question}]\ncontinue`])
})

test('Desktop navigator starts in the profile language and accepts an English navigation intent', async t => {
  const { view, reads, middleware, settle } = await surface(t, { quiz: null, navLanguage: 'zh', language: 'zh' })
  const button = view.getByRole('button', { name: '课堂导航' })
  assert.ok(reads.every(([, params]) => params.name !== 'teacher-desktop'))
  await settle(() => fireEvent.click(button))
  assert.equal(view.getByRole('heading', { name: '课堂导航' }).textContent, '课堂导航')
  assert.equal(view.getByRole('textbox', { name: '搜索或输入主题' }).value, '')
  const input = 'I’d like to study conditional probability'
  await settle(async () => assert.equal(await middleware({ text: input }), null))
  assert.ok(reads.some(([, params]) => params.name === 'teacher-desktop' &&
    JSON.parse(params.arg).action === 'prose' && JSON.parse(params.arg).argument === input))
})

test('hint preserves the multi-selection and note; unknown supports a note', async t => {
  const { view, sends, publish, settle } = await surface(t, { quiz: open('multi') })
  const boxes = view.getAllByRole('checkbox')
  await settle(() => {
    fireEvent.click(boxes[0])
    fireEvent.click(boxes[1])
    fireEvent.change(view.getByRole('textbox', { name: '推理备注（可选）' }), { target: { value: '仍有疑问' } })
    fireEvent.click(view.getByRole('button', { name: '提示' }))
  })
  assert.deepEqual(sends[0], ['lesson-a', `〔课堂作答 ${ids.question}〕\n提示`])
  await publish({ ...open('multi'), hint: '先看条件事件。' })
  assert.equal(view.getByText('先看条件事件。').textContent, '先看条件事件。')
  assert.deepEqual(boxes.map(box => box.checked), [true, true])
  assert.equal(view.getByRole('textbox', { name: '推理备注（可选）' }).value, '仍有疑问')
  assert.equal(view.getByRole('button', { name: '提交答案' }).disabled, false)
  await settle(() => fireEvent.click(view.getByRole('button', { name: '不知道' })))
  assert.deepEqual(sends[1], ['lesson-a', `〔课堂作答 ${ids.question}〕\n不知道\n仍有疑问`])
  const draft = view.getByRole('textbox', { name: '聊天输入' })
  draft.focus()
  await publish(feedback)
  assert.ok(document.activeElement === draft, '查看反馈时应保留正在输入的草稿焦点')
  await settle(() => fireEvent.keyDown(draft, { key: 'Enter' }))
  assert.equal(sends.length, 2)
})

test('cancel closes the question with either gateway reply format', async t => {
  const { view, sends, controls, settle } = await surface(t)
  controls.readOverride = () => reply(open(), 'plugin')
  await settle(() => fireEvent.click(view.getByRole('button', { name: '取消本题' })))
  assert.deepEqual(sends, [['lesson-a', `〔课堂作答 ${ids.question}〕\n取消`]])
})

test('multi-select sends the letters of stable choices in displayed order', async t => {
  const { view, sends, settle } = await surface(t, { quiz: open('multi') })
  const [first, second] = view.getAllByRole('checkbox')
  await settle(() => {
    fireEvent.click(second)
    fireEvent.click(first)
    fireEvent.click(view.getByRole('button', { name: '提交答案' }))
  })
  assert.deepEqual(sends, [['lesson-a', `〔课堂作答 ${ids.question}〕\nA B`]])
})

test('preference submits a selected option and additional free text together', async t => {
  const quiz = { ...open('multi'), kind: 'ask', options: [
    { id: '0', label: '逐步讲解' }, { id: '1', label: '先做练习' }
  ] }
  const { view, sends, settle } = await surface(t, { quiz })
  const card = within(view.getByRole('region', { name: '课堂问答' }))
  await settle(() => {
    fireEvent.click(card.getByRole('checkbox', { name: /^1\.\s*逐步讲解$/ }))
    fireEvent.change(card.getByRole('textbox', { name: '其他（自填）' }), { target: { value: '配图' } })
    fireEvent.click(card.getByRole('button', { name: '提交回答' }))
  })
  assert.deepEqual(sends, [['lesson-a', `〔课堂作答 ${ids.question}〕\n1 自填 配图`]])
})

test('a stale confirmation or a route switch during validation cannot submit', async t => {
  const { view, sends, controls, publish, settle } = await surface(t, { quiz: feedback })
  controls.quiz = open('single', ids.next)
  await settle(() => fireEvent.click(view.getByRole('button', { name: '继续' })))
  assert.deepEqual(sends, [])
  assert.equal(view.getByRole('region', { name: '课堂测验' }).dataset.teacherQuiz, 'open')

  await publish(feedback)
  let resolve
  controls.readOverride = () => new Promise(done => { resolve = done })
  await settle(() => fireEvent.click(view.getByRole('button', { name: '继续' })))
  await settle(() => {
    state.focusedSessionId.set('runtime-b')
    state.focusedStoredSessionId.set('ordinary-b')
    epoch.set(1)
  })
  await settle(() => resolve(reply(feedback)))
  assert.deepEqual(sends, [])
  assert.ok(!view.queryByRole('region', { name: '作答解析' }), '切课后应收起旧解析')
})

test('split composers stay empty while the focused quiz retains one status entry', async t => {
  const { main, view, reads, settle, polls } = await surface(t, { split: true })
  assert.equal(main.querySelector('#composer').childElementCount, 0)
  assert.equal(main.querySelector('#second-composer').childElementCount, 0)
  assert.equal(within(main.querySelector('#status')).getByRole('button', { name: '课堂测验' }).type, 'button')
  await settle(() => {
    state.focusedSessionId.set('ordinary-runtime')
    state.focusedStoredSessionId.set('ordinary')
  })
  assert.ok(!view.queryByRole('button', { name: '课堂测验' }), '普通会话应收起课堂控件')
  const beforePoll = reads.length
  await settle(() => { for (const poll of polls) poll() })
  assert.equal(reads.length, beforePoll)
})
