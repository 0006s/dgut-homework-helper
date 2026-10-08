// ==UserScript==
// @name         作业汇总助手 · 优学院
// @namespace    dgut-homework-helper
// @version      1.1.0
// @description  一键汇总优学院各课程最近要交的作业与待互评任务，按截止时间排序，一屏看全。
// @match        *://lms.dgut.edu.cn/*
// @match        *://application.dgut.edu.cn/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  'use strict'

  const VERSION = '1.1.0'
  const IS_TOP = (() => { try { return window.top === window } catch (e) { return false } })()
  try {
    console.log('[作业汇总] v' + VERSION + ' 已注入 | ' + (IS_TOP ? '顶层框架' : '子框架') + ' | ' + location.href)
  } catch (e) { /* 忽略 */ }

  const LMS = 'https://lms.dgut.edu.cn'
  const CACHE_KEY = 'hw_agg_cache_v1'
  const CFG_KEY = 'hw_agg_cfg_v1'
  const POS_KEY = 'hw_agg_pos_v1'
  const LOGIN_PAGE = 'https://application.dgut.edu.cn/application'
  const HOMEWORK_PAGE = function (courseId) {
    return LMS + '/courseweb/ulearning/index.html#/course/homework?courseId=' + encodeURIComponent(courseId)
  }
  const DEFAULT_CFG = { autoRefresh: true, showExpired: true, exclude: '', days: 14 }
  const HIDDEN_KEY = 'hw_agg_hidden_v1'
  const AUTO_REFRESH_MS = 10 * 60 * 1000

  function loadJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key)
      if (!raw) return fallback
      const v = JSON.parse(raw)
      return (v && typeof v === 'object') ? Object.assign({}, fallback, v) : fallback
    } catch (e) { return fallback }
  }
  function saveJSON(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)) } catch (e) { /* 忽略 */ }
  }

  let cfg = loadJSON(CFG_KEY, DEFAULT_CFG)

  function loadArr(key) {
    try {
      const v = JSON.parse(localStorage.getItem(key))
      return Array.isArray(v) ? v : []
    } catch (e) { return [] }
  }
  const hiddenKeys = new Set(loadArr(HIDDEN_KEY))
  function saveHidden() {
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(Array.from(hiddenKeys))) } catch (e) { /* 忽略 */ }
  }

  const state = {
    tab: 'todo',
    running: false,
    items: [],
    courses: [],
    failures: [],
    ignored: 0,
    courseCount: 0,
    updatedAt: 0,
    error: '',
  }

  const cache = loadJSON(CACHE_KEY, null)
  if (cache && Array.isArray(cache.items)) {
    state.items = cache.items
    state.courses = Array.isArray(cache.courses) ? cache.courses : []
    state.courseCount = cache.courseCount || state.courses.length
    state.updatedAt = cache.updatedAt || 0
  }

  // ==================== 工具 ====================
  function preview(s, n) {
    if (!s) return ''
    s = String(s)
    return s.length > n ? s.slice(0, n) + '…' : s
  }
  function pad2(x) { return String(x).padStart(2, '0') }
  function fmtDateTime(ms) {
    if (!ms) return ''
    const d = new Date(ms)
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes())
  }
  function fmtClock(ms) {
    const d = new Date(ms)
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds())
  }
  function fmtLeft(ms) {
    if (ms <= 0) {
      const over = -ms
      const d = Math.floor(over / 86400000)
      const h = Math.floor((over % 86400000) / 3600000)
      const m = Math.floor((over % 3600000) / 60000)
      if (d > 0) return '已过期 ' + d + '天' + h + '小时'
      if (h > 0) return '已过期 ' + h + '小时' + m + '分'
      return '已过期 ' + Math.max(1, m) + '分钟'
    }
    const d = Math.floor(ms / 86400000)
    const h = Math.floor((ms % 86400000) / 3600000)
    const m = Math.floor((ms % 3600000) / 60000)
    if (d > 0) return '还剩 ' + d + '天' + h + '小时'
    if (h > 0) return '还剩 ' + h + '小时' + m + '分'
    return '还剩 ' + Math.max(1, m) + '分'
  }

  // ==================== 接口 ====================
  function authHeaders() {
    const h = { 'Accept': 'application/json' }
    try {
      const m = /(?:^|;\s*)AUTHORIZATION=([^;]+)/.exec(document.cookie)
      if (m) {
        try { h['Authorization'] = decodeURIComponent(m[1]) } catch (e) { h['Authorization'] = m[1] }
      }
    } catch (e) { /* 忽略 */ }
    return h
  }

  async function apiGet(path, params) {
    const url = new URL(LMS + path)
    if (params) Object.keys(params).forEach(function (k) { url.searchParams.set(k, params[k]) })
    let res
    try {
      res = await fetch(url.href, { credentials: 'include', headers: authHeaders() })
    } catch (e) {
      const err = new Error(location.host === 'lms.dgut.edu.cn' ? '网络错误，请求未发出' : '跨域被拦截：优学院接口只能在 lms.dgut.edu.cn 页面上访问')
      err.cors = true
      throw err
    }
    if (res.status === 401 || res.status === 403) {
      const err = new Error('HTTP ' + res.status + '，未登录或登录已失效')
      err.auth = true
      throw err
    }
    if (!res.ok) {
      const err = new Error('HTTP ' + res.status)
      err.http = res.status
      throw err
    }
    return res.json()
  }

  function bfsList(payload, keys) {
    const queue = [payload]
    let guard = 0
    while (queue.length && guard++ < 8) {
      const c = queue.shift()
      if (Array.isArray(c)) return c.filter(function (x) { return x && typeof x === 'object' })
      if (!c || typeof c !== 'object') continue
      for (let i = 0; i < keys.length; i++) {
        if (Array.isArray(c[keys[i]])) {
          return c[keys[i]].filter(function (x) { return x && typeof x === 'object' })
        }
      }
      ;['result', 'data', 'rows'].forEach(function (k) {
        if (c[k] && typeof c[k] === 'object') queue.push(c[k])
      })
    }
    return []
  }
  function pickList(payload, specificKey) {
    const specific = bfsList(payload, [specificKey])
    if (specific.length) return specific
    return bfsList(payload, ['list', 'records'])
  }
  function looksAuthError(payload) {
    try {
      const t = JSON.stringify(payload || '').slice(0, 4000)
      if (/courseList|homeworkList/.test(t)) return false
      return /未登录|登录失效|请先登录|登录已过期|无权限|授权失效/.test(t)
    } catch (e) { return false }
  }

  async function fetchCourses() {
    const all = []
    const seen = new Set()
    for (let pn = 1; pn <= 5; pn++) {
      const payload = await apiGet('/courseapi/courses/students', {
        keyword: '', publishStatus: 1, type: 1, pn: pn, ps: 50,
      })
      if (looksAuthError(payload)) {
        const err = new Error('登录已失效')
        err.auth = true
        throw err
      }
      const page = pickList(payload, 'courseList')
      page.forEach(function (c) {
        if (!c || c.id === undefined || c.id === null) return
        const id = String(c.id)
        if (seen.has(id)) return
        seen.add(id)
        all.push({
          id: id,
          name: String(c.name || '未命名课程'),
          teacher: String(c.teacherName || ''),
        })
      })
      if (page.length < 50) break
    }
    return all
  }

  async function fetchHomeworks(course) {
    const payload = await apiGet('/courseapi/homeworks/student/v2', {
      ocId: course.id, pn: 1, ps: 999, lang: 'zh',
    })
    if (looksAuthError(payload)) {
      const err = new Error('登录已失效')
      err.auth = true
      throw err
    }
    return pickList(payload, 'homeworkList')
  }

  // ==================== 解析 ====================
  function toMs(v) {
    if (v === null || v === undefined || v === '') return 0
    if (typeof v === 'number' || (typeof v === 'string' && /^\d+$/.test(String(v).trim()))) {
      let n = Number(v)
      if (!isFinite(n) || n <= 0) return 0
      if (n < 1e11) n *= 1000
      return n
    }
    const t = Date.parse(String(v))
    return isNaN(t) ? 0 : t
  }
  function firstDefined(obj, keys) {
    for (let i = 0; i < keys.length; i++) {
      const v = obj[keys[i]]
      if (v !== undefined && v !== null && v !== '') return v
    }
    return undefined
  }
  function kindOf(stateVal, rawState) {
    if (stateVal === 0) return 'todo'
    if (stateVal === 1 || stateVal === 2) return 'review'
    if (typeof rawState === 'string') {
      if (/已提交|已批|已完成|已互评/.test(rawState)) return null
      if (/未提交|待提交/.test(rawState)) return 'todo'
      if (/互评/.test(rawState)) return 'review'
    }
    return null
  }
  function normalizeItem(item, course) {
    if (!item || typeof item !== 'object') return null
    const rawState = firstDefined(item, ['state', 'homeworkState', 'status'])
    let stateVal = NaN
    if (typeof rawState === 'number') stateVal = rawState
    else if (rawState !== undefined) stateVal = parseInt(rawState, 10)
    const kind = kindOf(isNaN(stateVal) ? null : stateVal, rawState)
    if (!kind) return null
    const endRaw = firstDefined(item, ['endTime', 'deadline', 'dueTime', 'end_time', 'closeTime', 'submitDeadline'])
    const title = String(firstDefined(item, ['homeworkTitle', 'title', 'name']) || '未命名作业').trim()
    const idRaw = firstDefined(item, ['id', 'homeworkId', 'relationId'])
    return {
      key: (idRaw !== undefined ? String(idRaw) : title) + '|' + course.id,
      id: idRaw !== undefined ? String(idRaw) : '',
      title: title,
      kind: kind,
      state: isNaN(stateVal) ? null : stateVal,
      rawState: rawState === undefined ? '' : String(rawState),
      endMs: toMs(endRaw),
      courseId: course.id,
      courseName: course.name,
      teacher: course.teacher,
    }
  }

  // ==================== 刷新 ====================
  function persist() {
    saveJSON(CACHE_KEY, {
      items: state.items,
      courses: state.courses,
      courseCount: state.courseCount,
      updatedAt: state.updatedAt,
    })
  }

  async function refresh() {
    if (state.running) return
    state.running = true
    state.error = ''
    renderStatus()
    const t0 = Date.now()
    try {
      const courses = await fetchCourses()
      state.courses = courses
      state.courseCount = courses.length
      if (!courses.length) {
        state.items = []
        state.failures = []
        state.updatedAt = Date.now()
        state.error = '没有读到课程列表。请确认已登录、且本学期已选课。'
        persist()
        log('课程列表为空（未登录或本学期无课程）', 'err')
        return
      }
      const results = await Promise.all(courses.map(async function (c) {
        try {
          return { course: c, list: await fetchHomeworks(c) }
        } catch (e) {
          return { course: c, error: e }
        }
      }))
      const items = []
      const failures = []
      const ignoredStates = new Set()
      let authFailed = 0
      results.forEach(function (r) {
        if (r.error) {
          failures.push(r.course.name + '：' + r.error.message)
          if (r.error.auth) authFailed++
          return
        }
        const seen = new Set()
        r.list.forEach(function (it) {
          const n = normalizeItem(it, r.course)
          if (!n) {
            if (it && typeof it === 'object') {
              const raw = firstDefined(it, ['state', 'homeworkState', 'status'])
              if (raw !== undefined && raw !== null && raw !== '') ignoredStates.add(String(raw))
            }
            return
          }
          if (seen.has(n.key)) return
          seen.add(n.key)
          items.push(n)
        })
      })
      items.sort(function (a, b) {
        const ka = a.endMs || Number.MAX_SAFE_INTEGER
        const kb = b.endMs || Number.MAX_SAFE_INTEGER
        if (ka !== kb) return ka - kb
        return a.courseName.localeCompare(b.courseName, 'zh')
      })
      state.items = items
      state.failures = failures
      state.ignored = ignoredStates.size
      state.updatedAt = Date.now()
      persist()
      const todo = countKind('todo')
      const review = countKind('review')
      const hid = hiddenCount()
      log('读取 ' + courses.length + ' 门课，待提交 ' + todo + ' · 待互评 ' + review +
        '，用时 ' + ((Date.now() - t0) / 1000).toFixed(1) + 's' +
        (hid ? '（另过滤掉 ' + hid + ' 条：隐藏/关键词/天数）' : ''), 'ok')
      if (ignoredStates.size) {
        log('已忽略 ' + ignoredStates.size + ' 种其他状态的作业（state=' +
          Array.from(ignoredStates).slice(0, 8).join(',') + '），已提交/已批阅的不在此列', '')
      }
      if (failures.length) {
        if (authFailed === failures.length) {
          state.error = '登录已失效，请重新登录优学院后再刷新'
          log('全部读取失败：' + preview(failures[0], 120), 'err')
        } else {
          log(failures.length + ' 门课读取失败：' + preview(failures[0], 120), 'err')
          if (failures.some(function (f) { return f.indexOf('跨域') >= 0 })) {
            state.error = '部分请求被跨域拦截，请到 lms.dgut.edu.cn 页面使用'
          }
        }
      }
    } catch (e) {
      state.error = e && e.auth ? '登录已失效，请重新登录优学院后再刷新' : (e ? e.message : '未知错误')
      log('刷新失败：' + state.error, 'err')
      if (e && e.auth) log('打开 ' + LOGIN_PAGE + ' 重新登录后再点刷新', 'err')
    } finally {
      state.running = false
      renderAll()
    }
  }

  function excludedByKeyword(item) {
    const kw = String(cfg.exclude || '').trim()
    if (!kw) return false
    const words = kw.split(/[,，\s]+/).filter(Boolean)
    const title = item.title.toLowerCase()
    for (let i = 0; i < words.length; i++) {
      if (title.indexOf(words[i].toLowerCase()) >= 0) return true
    }
    return false
  }
  function isVisible(item) {
    if (hiddenKeys.has(item.key)) return false
    if (excludedByKeyword(item)) return false
    const days = Number(cfg.days) || 0
    if (days > 0 && item.endMs && item.endMs - Date.now() > days * 86400000) return false
    return true
  }
  function countKind(kind) {
    let n = 0
    for (let i = 0; i < state.items.length; i++) {
      if (state.items[i].kind === kind && isVisible(state.items[i])) n++
    }
    return n
  }
  function hiddenCount() {
    return state.items.filter(function (i) { return !isVisible(i) }).length
  }

  // ==================== 界面 ====================
  let ui = null
  let countdownNodes = []

  function buildUI() {
    const host = document.createElement('div')
    host.id = 'hwa-host'
    host.style.cssText = [
      'position:fixed', 'z-index:2147483646', 'right:16px', 'bottom:16px',
      'left:auto', 'top:auto', 'margin:0', 'padding:0', 'border:0',
      'background:transparent', 'width:auto', 'height:auto', 'overflow:visible',
      'visibility:visible', 'opacity:1', 'transform:none', 'filter:none',
    ].join(';') + ';'
    let root = host
    try {
      if (host.attachShadow) root = host.attachShadow({ mode: 'open' })
    } catch (e) { root = host }

    const style = document.createElement('style')
    style.textContent = [
      '*{box-sizing:border-box;font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}',
      '.panel{width:400px;max-height:78vh;display:flex;flex-direction:column;background:#1b1d23;color:#e6e6e6;',
      'border:1px solid #3a3d46;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.5);font-size:12px;overflow:hidden}',
      '.hd{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#23262e;cursor:move;user-select:none}',
      '.hd b{font-size:13px;flex:1}',
      '.hd button{background:none;border:none;color:#9aa0ab;cursor:pointer;font-size:13px;padding:2px 8px;border-radius:4px}',
      '.hd button:hover{background:#2f3440;color:#e6e6e6}',
      '.bd{overflow:auto;padding:10px;display:flex;flex-direction:column;gap:10px}',
      '.status{padding:5px 8px;border-radius:6px;background:#15171c;border:1px solid #2c303a;text-align:center;font-weight:600;word-break:break-all}',
      '.tabs{display:flex;gap:6px;align-items:center;flex-wrap:wrap}',
      '.tab{background:#20232b;border:1px solid #343843;color:#9aa0ab;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px}',
      '.tab.on{background:#141d2e;border-color:#1f6feb;color:#79b8ff;font-weight:600}',
      '.opts{display:flex;gap:12px;flex-wrap:wrap;color:#7d8590;font-size:11px}',
      '.opts label{display:flex;align-items:center;gap:4px;cursor:pointer}',
      '.row{border:1px solid #333742;border-radius:6px;padding:6px 8px;background:#191b21;cursor:pointer}',
      '.row:hover{border-color:#4a5060}',
      '.r1{display:flex;gap:6px;align-items:baseline}',
      '.badge{flex:0 0 auto;padding:0 5px;border-radius:3px;font-size:10px;font-weight:600}',
      '.badge.todo{background:#4a3a12;color:#e3b341}',
      '.badge.review{background:#1f2a4a;color:#79b8ff}',
      '.ttl{flex:1;word-break:break-all;color:#e6e6e6}',
      '.cd{flex:0 0 auto;color:#d29922;white-space:nowrap;font-weight:600}',
      '.cd.over{color:#ff7b72}',
      '.cd.none{color:#6e7681;font-weight:400}',
      '.hbtn{flex:0 0 auto;background:none;border:none;color:#57606a;cursor:pointer;font-size:15px;line-height:1;padding:0 3px;opacity:0;border-radius:4px}',
      '.row:hover .hbtn{opacity:1}',
      '.hbtn:hover{color:#ff7b72;background:#2c303a}',
      '.opts .grow{flex:1;min-width:150px;display:flex;align-items:center;gap:6px}',
      '.opts input[type=text]{flex:1;min-width:0;background:#15171c;border:1px solid #3a3d46;color:#e6e6e6;border-radius:5px;padding:3px 6px;font-size:11px}',
      '.opts input[type=number]{width:58px;background:#15171c;border:1px solid #3a3d46;color:#e6e6e6;border-radius:5px;padding:3px 6px;font-size:11px}',
      'button:disabled{opacity:.45;cursor:default}',
      '.r2{color:#7d8590;margin-top:3px;font-size:11px;word-break:break-all}',
      '.empty{color:#7d8590;text-align:center;padding:14px 6px;line-height:1.7}',
      '.fail{color:#d29922;font-size:11px;margin-top:6px;word-break:break-all}',
      '.sec{border:1px solid #343843;border-radius:8px;padding:8px;background:#20232b}',
      '.sec h4{margin:0 0 6px;font-size:12px;color:#8fb7ff;font-weight:600}',
      '#hwaLog{max-height:110px;overflow:auto;background:#111318;border:1px solid #2c303a;border-radius:6px;',
      'padding:6px;font-family:ui-monospace,Consolas,monospace;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-all}',
      '.ok{color:#7ee787}.err{color:#ff7b72}',
      '.mini{color:#7d8590;font-size:11px;line-height:1.5}',
      '.collapsed .bd{display:none}',
    ].join('')

    root.appendChild(style)
    const panel = document.createElement('div')
    panel.className = 'panel'
    panel.innerHTML = [
      '<div class="hd" id="hwaHd"><b>作业汇总</b><button id="hwaBtnRefresh" title="重新拉取全部课程作业">刷新</button><button id="hwaBtnMin" title="折叠">–</button></div>',
      '<div class="bd">',
      '  <div class="status" id="hwaStatus">加载中…</div>',
      '  <div class="tabs">',
      '    <button class="tab" data-tab="todo" id="hwaTabTodo">待提交 <span id="hwaCntTodo">0</span></button>',
      '    <button class="tab" data-tab="review" id="hwaTabReview">待互评 <span id="hwaCntReview">0</span></button>',
      '  </div>',
      '  <div class="opts">',
      '    <label><input type="checkbox" id="hwaShowExpired"> 显示已过期</label>',
      '    <label><input type="checkbox" id="hwaAuto"> 每 10 分钟自动刷新</label>',
      '    <button class="tab" id="hwaBtnUnhide" title="恢复所有手动隐藏的条目">恢复隐藏 (0)</button>',
      '  </div>',
      '  <div class="opts">',
      '    <label class="grow"><span>排除关键词</span><input type="text" id="hwaExclude" placeholder="标题含这些词就不显示，逗号分隔"></label>',
      '    <label title="只显示这么多天内截止的作业，留空或 0 表示不限"><input type="number" id="hwaDays" min="0" step="1" placeholder="不限"> 天内截止</label>',
      '  </div>',
      '  <div id="hwaList"></div>',
      '  <div class="sec"><h4>日志</h4><div id="hwaLog"></div></div>',
      '</div>',
    ].join('\n')
    root.appendChild(panel)

    let mountMode = 'normal'
    try {
      if (typeof host.showPopover === 'function') {
        host.setAttribute('popover', 'manual')
        ;(document.body || document.documentElement).appendChild(host)
        host.showPopover()
        mountMode = 'popover'
      } else {
        ;(document.documentElement || document.body).appendChild(host)
      }
    } catch (e) {
      mountMode = 'normal'
      try { (document.documentElement || document.body).appendChild(host) } catch (e2) { /* 忽略 */ }
    }
    try {
      console.log('[作业汇总] 面板已挂载（' + mountMode + '）')
    } catch (e) { /* 忽略 */ }

    const $ = function (id) { return root.querySelector('#' + id) }
    ui = {
      host: host, panel: panel,
      status: $('hwaStatus'), list: $('hwaList'), logBox: $('hwaLog'),
      cntTodo: $('hwaCntTodo'), cntReview: $('hwaCntReview'),
      tabTodo: $('hwaTabTodo'), tabReview: $('hwaTabReview'),
      showExpired: $('hwaShowExpired'), auto: $('hwaAuto'),
      exclude: $('hwaExclude'), unhide: $('hwaBtnUnhide'), days: $('hwaDays'),
    }

    ui.showExpired.checked = cfg.showExpired !== false
    ui.auto.checked = cfg.autoRefresh !== false
    ui.showExpired.addEventListener('change', function () {
      cfg.showExpired = ui.showExpired.checked
      saveJSON(CFG_KEY, cfg)
      renderList()
    })
    ui.auto.addEventListener('change', function () {
      cfg.autoRefresh = ui.auto.checked
      saveJSON(CFG_KEY, cfg)
    })
    ui.exclude.value = cfg.exclude || ''
    ui.exclude.addEventListener('input', function () {
      cfg.exclude = ui.exclude.value
      saveJSON(CFG_KEY, cfg)
      renderAll()
    })
    ui.days.value = (Number(cfg.days) || 0) > 0 ? cfg.days : ''
    ui.days.addEventListener('input', function () {
      const n = parseInt(ui.days.value, 10)
      cfg.days = isFinite(n) && n > 0 ? n : 0
      saveJSON(CFG_KEY, cfg)
      renderAll()
    })
    ui.days.addEventListener('change', function () {
      if ((Number(cfg.days) || 0) > 0) ui.days.value = cfg.days
    })
    ui.unhide.addEventListener('click', function () {
      if (!hiddenKeys.size) return
      hiddenKeys.clear()
      saveHidden()
      log('已恢复全部隐藏的条目', 'ok')
      renderAll()
    })

    const switchTab = function (tab) {
      state.tab = tab
      renderAll()
    }
    ui.tabTodo.addEventListener('click', function () { switchTab('todo') })
    ui.tabReview.addEventListener('click', function () { switchTab('review') })
    $('hwaBtnRefresh').addEventListener('click', function () { refresh() })
    $('hwaBtnMin').addEventListener('click', function (e) {
      e.stopPropagation()
      panel.classList.toggle('collapsed')
    })

    const pos = loadJSON(POS_KEY, null)
    if (pos && typeof pos.left === 'number' && typeof pos.top === 'number') {
      host.style.right = 'auto'
      host.style.bottom = 'auto'
      host.style.left = pos.left + 'px'
      host.style.top = pos.top + 'px'
    }
    let drag = null
    $('hwaHd').addEventListener('mousedown', function (e) {
      const r = panel.getBoundingClientRect()
      drag = { dx: e.clientX - r.left, dy: e.clientY - r.top }
      host.style.right = 'auto'; host.style.bottom = 'auto'
      host.style.left = r.left + 'px'; host.style.top = r.top + 'px'
      e.preventDefault()
    })
    window.addEventListener('mousemove', function (e) {
      if (!drag) return
      host.style.left = Math.max(0, e.clientX - drag.dx) + 'px'
      host.style.top = Math.max(0, e.clientY - drag.dy) + 'px'
    })
    window.addEventListener('mouseup', function () {
      if (!drag) return
      drag = null
      try {
        const r = host.getBoundingClientRect()
        saveJSON(POS_KEY, { left: Math.round(r.left), top: Math.round(r.top) })
      } catch (e) { /* 忽略 */ }
    })

    log('已就绪。点击「刷新」拉取全部课程的作业。', 'ok')
    if (state.updatedAt) {
      log('已加载缓存（' + fmtDateTime(state.updatedAt) + '），共 ' + state.items.length + ' 条待办', '')
    }
    renderAll()

    if (!state.updatedAt || Date.now() - state.updatedAt > 60000) refresh()
  }

  function countdownText(item) {
    if (!item.endMs) return '无截止时间'
    return fmtLeft(item.endMs - Date.now())
  }

  function renderStatus() {
    if (!ui) return
    let s
    if (state.running) {
      s = '⏳ 刷新中…'
      ui.status.style.color = '#d29922'
    } else if (state.error) {
      s = state.error
      ui.status.style.color = '#ff7b72'
    } else if (state.updatedAt) {
      s = state.courseCount + ' 门课 · 待提交 ' + countKind('todo') + ' · 待互评 ' + countKind('review') +
        ' · ' + fmtClock(state.updatedAt) + ' 更新'
      ui.status.style.color = '#7ee787'
    } else {
      s = '尚未加载'
      ui.status.style.color = '#e6e6e6'
    }
    ui.status.textContent = s
    ui.cntTodo.textContent = countKind('todo')
    ui.cntReview.textContent = countKind('review')
    ui.tabTodo.classList.toggle('on', state.tab === 'todo')
    ui.tabReview.classList.toggle('on', state.tab === 'review')
    ui.unhide.textContent = '恢复隐藏 (' + hiddenKeys.size + ')'
    ui.unhide.disabled = hiddenKeys.size === 0
  }

  function renderList() {
    if (!ui) return
    countdownNodes = []
    const now = Date.now()
    const showExpired = cfg.showExpired !== false
    const items = state.items.filter(function (it) {
      if (it.kind !== state.tab) return false
      if (!isVisible(it)) return false
      if (!showExpired && it.endMs && it.endMs < now) return false
      return true
    })
    ui.list.innerHTML = ''
    if (!state.items.length) {
      const d = document.createElement('div')
      d.className = 'empty'
      d.innerHTML = state.updatedAt
        ? '当前没有' + (state.tab === 'todo' ? '待提交' : '待互评') + '的作业<br><span class="mini">切换上方标签查看另一类</span>'
        : '还没有数据，点右上角「刷新」'
      ui.list.appendChild(d)
      return
    }
    if (!items.length) {
      const d = document.createElement('div')
      d.className = 'empty'
      d.innerHTML = '没有可显示的条目<br><span class="mini">可能是已过期被隐藏、被关键词排除、或超出天数限制，可点「恢复隐藏」、清空关键词、调大天数</span>'
      ui.list.appendChild(d)
      return
    }
    items.forEach(function (item) {
      const row = document.createElement('div')
      row.className = 'row'
      const r1 = document.createElement('div')
      r1.className = 'r1'
      const badge = document.createElement('span')
      badge.className = 'badge ' + item.kind
      badge.textContent = item.kind === 'todo' ? '待提交' : (item.state === 2 ? '互评中' : '待互评')
      const ttl = document.createElement('span')
      ttl.className = 'ttl'
      ttl.textContent = item.title
      const cd = document.createElement('span')
      const expired = !!(item.endMs && item.endMs < now)
      cd.className = 'cd' + (expired ? ' over' : (item.endMs ? '' : ' none'))
      cd.textContent = countdownText(item)
      r1.appendChild(badge)
      r1.appendChild(ttl)
      r1.appendChild(cd)
      const hb = document.createElement('button')
      hb.className = 'hbtn'
      hb.textContent = '×'
      hb.title = '不再显示这条'
      hb.addEventListener('click', function (e) {
        e.stopPropagation()
        hiddenKeys.add(item.key)
        saveHidden()
        log('已隐藏：' + preview(item.title, 40) + '（点「恢复隐藏」可找回）', '')
        renderAll()
      })
      r1.appendChild(hb)
      const r2 = document.createElement('div')
      r2.className = 'r2'
      r2.textContent = item.courseName + (item.teacher ? ' · ' + item.teacher : '') +
        (item.endMs ? ' · 截止 ' + fmtDateTime(item.endMs) : '')
      row.appendChild(r1)
      row.appendChild(r2)
      row.title = '点击在新标签打开该课程的作业页'
      row.addEventListener('click', function () {
        window.open(HOMEWORK_PAGE(item.courseId), '_blank')
      })
      ui.list.appendChild(row)
      countdownNodes.push({ node: cd, item: item })
    })
    if (state.failures.length) {
      const f = document.createElement('div')
      f.className = 'fail'
      f.textContent = '⚠ ' + state.failures.length + ' 门课读取失败：' + preview(state.failures[0], 100)
      ui.list.appendChild(f)
    }
  }

  function renderAll() {
    renderStatus()
    renderList()
  }

  function log(msg, cls) {
    if (!ui) { console.log('[作业汇总]', msg); return }
    const line = document.createElement('div')
    if (cls) line.className = cls
    line.textContent = '[' + new Date().toLocaleTimeString('zh-CN', { hour12: false }) + '] ' + msg
    ui.logBox.appendChild(line)
    ui.logBox.scrollTop = ui.logBox.scrollHeight
    while (ui.logBox.childNodes.length > 100) ui.logBox.removeChild(ui.logBox.firstChild)
  }

  // ==================== 定时 ====================
  setInterval(function () {
    if (!ui) return
    countdownNodes.forEach(function (n) {
      if (!n.node.isConnected) return
      const txt = countdownText(n.item)
      if (n.node.textContent !== txt) n.node.textContent = txt
      const expired = !!(n.item.endMs && n.item.endMs < Date.now())
      const want = 'cd' + (expired ? ' over' : (n.item.endMs ? '' : ' none'))
      if (n.node.className !== want) n.node.className = want
    })
  }, 30000)

  setInterval(function () {
    if (!cfg.autoRefresh || !ui) return
    if (document.visibilityState !== 'visible') return
    if (Date.now() - state.updatedAt < AUTO_REFRESH_MS) return
    refresh()
  }, 60000)

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !ui) return
    if (Date.now() - state.updatedAt > 5 * 60 * 1000) refresh()
  })

  // ==================== 控制台调试 ====================
  try {
    window.__HW_AGG__ = {
      version: VERSION,
      state: state,
      cfg: cfg,
      refresh: refresh,
      items: function () { return state.items.slice() },
      courses: function () { return state.courses.slice() },
      counts: function () { return { todo: countKind('todo'), review: countKind('review'), total: state.items.length } },
      pickList: pickList,
      toMs: toMs,
      normalize: normalizeItem,
      isVisible: isVisible,
      hiddenKeys: function () { return Array.from(hiddenKeys) },
      hide: function (key) { hiddenKeys.add(key); saveHidden(); renderAll() },
      unhideAll: function () { hiddenKeys.clear(); saveHidden(); renderAll() },
      coursesRaw: function () { return fetchCourses() },
      homeworksRaw: function (courseId) {
        const c = state.courses.find(function (x) { return String(x.id) === String(courseId) })
        if (!c) { console.log('[作业汇总] 没有这门课，先 refresh()'); return Promise.resolve(null) }
        return fetchHomeworks(c)
      },
    }
  } catch (e) { /* 忽略 */ }

  // ==================== 启动 ====================
  const mount = function () {
    if (!document.documentElement) return setTimeout(mount, 50)
    if (document.getElementById('hwa-host')) return
    buildUI()
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount)
  else mount()

  setInterval(function () {
    if (!ui) return
    const h = ui.host
    try {
      if (h.getAttribute('popover') !== null) {
        if (!h.matches(':popover-open')) h.showPopover()
        return
      }
    } catch (e) { /* 引擎不支持 */ }
    try {
      if (!document.documentElement.contains(h)) document.documentElement.appendChild(h)
    } catch (e) { /* 忽略 */ }
  }, 2000)
})()
