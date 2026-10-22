// ==UserScript==
// @name         作业汇总助手 · 优学院
// @namespace    dgut-homework-helper
// @version      1.2.1
// @description  汇总优学院作业、互评和考试，支持考试倒计时、临考高亮与课程考试页跳转。
// @match        *://lms.dgut.edu.cn/*
// @match        *://application.dgut.edu.cn/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  'use strict'

  const VERSION = '1.2.1'
  const IS_TOP = (() => { try { return window.top === window } catch (e) { return false } })()
  try {
    const authCookie = /(?:^|;\s*)AUTHORIZATION=([^;]+)/.exec(document.cookie)
    if (authCookie && !window.Authorization) {
      try { window.Authorization = decodeURIComponent(authCookie[1]) } catch (e) { window.Authorization = authCookie[1] }
    }
  } catch (e) { /* 考试页由 Cookie 初始化自己的 Authorization */ }
  try {
    console.log('[作业汇总] v' + VERSION + ' 已注入 | ' + (IS_TOP ? '顶层框架' : '子框架') + ' | ' + location.href)
  } catch (e) { /* 忽略 */ }
  if (/^\/apps\/exam(?:\/|$)/.test(location.pathname)) return

  const LMS = 'https://lms.dgut.edu.cn'
  const CACHE_KEY = 'hw_agg_cache_v2'
  const CFG_KEY = 'hw_agg_cfg_v1'
  const POS_KEY = 'hw_agg_pos_v1'
  const LOGIN_PAGE = 'https://application.dgut.edu.cn/application'
  const EXAM_BASE = LMS + '/apps/exam/'
  const HOMEWORK_PAGE = function (courseId) {
    return LMS + '/courseweb/ulearning/index.html#/course/homework?courseId=' + encodeURIComponent(courseId)
  }
  const COURSE_EXAM_PAGE = function (courseId) {
    return LMS + '/courseweb/ulearning/index.html#/course/exam?courseId=' + encodeURIComponent(courseId)
  }
  const DEFAULT_CFG = {
    autoRefresh: true,
    showExpired: true,
    exclude: '',
    days: 14,
    examShowEnded: false,
  }
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
    exams: [],
    courses: [],
    failures: [],
    examFailures: [],
    ignored: 0,
    courseCount: 0,
    userId: '',
    examError: '',
    updatedAt: 0,
    error: '',
  }

  const cache = loadJSON(CACHE_KEY, null)
  if (cache && Array.isArray(cache.items)) {
    state.items = cache.items
    state.exams = Array.isArray(cache.exams) ? cache.exams : []
    state.courses = Array.isArray(cache.courses) ? cache.courses : []
    state.courseCount = cache.courseCount || state.courses.length
    state.examError = cache.examError || ''
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
  function fmtExamClock(ms) {
    const sec = Math.max(0, Math.floor(ms / 1000))
    const h = Math.floor(sec / 3600)
    const m = Math.floor((sec % 3600) / 60)
    const s = sec % 60
    return pad2(h) + ':' + pad2(m) + ':' + pad2(s)
  }
  function cookieValue(name) {
    try {
      const raw = document.cookie || ''
      const parts = raw.split(';')
      for (let i = 0; i < parts.length; i++) {
        const p = parts[i].trim()
        if (p.slice(0, name.length + 1) !== name + '=') continue
        const value = p.slice(name.length + 1)
        try { return decodeURIComponent(value) } catch (e) { return value }
      }
    } catch (e) { /* 忽略 */ }
    return ''
  }
  function parseMaybeJSON(value) {
    if (value && typeof value === 'object') return value
    if (typeof value !== 'string' || !value.trim()) return null
    let text = value.trim()
    for (let i = 0; i < 2; i++) {
      try { return JSON.parse(text) } catch (e) { /* 继续尝试解码 */ }
      try {
        const decoded = decodeURIComponent(text)
        if (decoded === text) break
        text = decoded
      } catch (e) { break }
    }
    return null
  }
  function extractUserId(value, depth) {
    depth = depth || 0
    if (depth > 5 || value === null || value === undefined) return ''
    if (typeof value === 'string') {
      const parsed = parseMaybeJSON(value)
      return parsed ? extractUserId(parsed, depth + 1) : ''
    }
    if (typeof value !== 'object') return ''
    if (Array.isArray(value)) {
      for (let i = 0; i < Math.min(value.length, 5); i++) {
        const found = extractUserId(value[i], depth + 1)
        if (found) return found
      }
      return ''
    }
    const direct = firstDefined(value, ['userId', 'userID'])
    if (direct !== undefined && /^\d+$/.test(String(direct)) && Number(direct) > 0) return String(direct)
    if (value.id !== undefined && (value.roleId || value.loginName || value.name)) {
      const id = String(value.id)
      if (/^\d+$/.test(id) && Number(id) > 0) return id
    }
    const containers = ['result', 'data', 'userInfo', 'user', 'state']
    for (let i = 0; i < containers.length; i++) {
      if (value[containers[i]] !== undefined) {
        const found = extractUserId(value[containers[i]], depth + 1)
        if (found) return found
      }
    }
    return ''
  }
  function currentUserId() {
    if (state.userId && /^\d+$/.test(String(state.userId))) return String(state.userId)
    try {
      const query = new URLSearchParams(location.search)
      const fromSearch = query.get('userid') || query.get('userID') || query.get('userId')
      if (fromSearch && /^\d+$/.test(fromSearch)) return fromSearch
      const hash = String(location.hash || '').replace(/^#/, '')
      const qIndex = hash.indexOf('?')
      if (qIndex >= 0) {
        const hashQuery = new URLSearchParams(hash.slice(qIndex + 1))
        const fromHash = hashQuery.get('userid') || hashQuery.get('userID') || hashQuery.get('userId')
        if (fromHash && /^\d+$/.test(fromHash)) return fromHash
      }
    } catch (e) { /* 忽略 */ }
    const sources = []
    try { sources.push(window.USER_INFO, window.userInfo, window.__USER_INFO__) } catch (e) { /* 忽略 */ }
    try { sources.push(window.$store && window.$store.state && window.$store.state.user) } catch (e) { /* 忽略 */ }
    try { sources.push(decodeURIComponent(location.hash.replace(/^#/, ''))) } catch (e) { /* 忽略 */ }
    sources.push(cookieValue('USER_INFO'), cookieValue('userId'), cookieValue('userID'))
    const stores = []
    try { stores.push(localStorage, sessionStorage) } catch (e) { /* 忽略 */ }
    stores.forEach(function (store) {
      try {
        for (let i = 0; i < store.length; i++) {
          const key = store.key(i) || ''
          if (/USER|user|login|account/i.test(key)) sources.push(store.getItem(key))
        }
      } catch (e) { /* 忽略 */ }
    })
    for (let i = 0; i < sources.length; i++) {
      const found = extractUserId(sources[i])
      if (found) {
        state.userId = found
        return found
      }
    }
    return ''
  }
  function runtimeDeviceId() {
    try { if (window.deviceID) return String(window.deviceID) } catch (e) { /* 忽略 */ }
    try { return sessionStorage.getItem('deviceID') || localStorage.getItem('deviceID') || '' } catch (e) { return '' }
  }

  // ==================== 接口 ====================
  function authHeaders() {
    const h = { 'Accept': 'application/json' }
    try {
      const token = cookieValue('AUTHORIZATION') || window.Authorization || ''
      if (token) h['Authorization'] = token
    } catch (e) { /* 忽略 */ }
    return h
  }

  async function apiGetUrl(url, params, extraHeaders) {
    if (params) Object.keys(params).forEach(function (k) { url.searchParams.set(k, params[k]) })
    let res
    try {
      const headers = Object.assign(authHeaders(), extraHeaders || {})
      res = await fetch(url.href, { credentials: 'include', headers: headers })
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
  function apiGet(path, params) {
    return apiGetUrl(new URL(LMS + path), params)
  }
  function examApiGet(path, params) {
    const headers = {
      clientId: runtimeDeviceId(),
      lang: 'zh-CN',
    }
    try { if (window.traceId) headers.traceId = String(window.traceId) } catch (e) { /* 忽略 */ }
    return apiGetUrl(new URL(EXAM_BASE + path), params, headers)
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
      if (/courseList|homeworkList|examArr/.test(t)) return false
      return /未登录|登录失效|请先登录|登录已过期|无权限|授权失效|缺少访问token/i.test(t)
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

  async function fetchExams(course, userId) {
    const all = []
    const seen = new Set()
    for (let page = 1; page <= 10; page++) {
      const payload = await examApiGet('getExamList', {
        userID: userId,
        ocId: course.id,
        intPage: page,
      })
      if (looksAuthError(payload)) {
        const err = new Error('登录已失效')
        err.auth = true
        throw err
      }
      const list = pickList(payload, 'examArr')
      let added = 0
      list.forEach(function (exam) {
        if (!exam || typeof exam !== 'object') return
        const id = firstDefined(exam, ['examID', 'examId', 'id'])
        const key = id !== undefined ? String(id) : String(firstDefined(exam, ['title', 'name']) || '') + '|' + course.id
        if (seen.has(key)) return
        seen.add(key)
        all.push(exam)
        added++
      })
      if (list.length < 10 || !added) break
    }
    return all
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
  function examTypeLabel(examType) {
    const n = parseInt(examType, 10)
    const labels = {
      1: '电脑',
      2: '手机',
      3: '手机/电脑/客户端',
      4: '电脑客户端',
      5: '手机/电脑',
      6: '电脑/客户端',
      7: '手机/客户端',
      8: '手机',
      9: '手机',
      10: '电脑',
      11: '电脑',
      12: '手机/电脑',
      13: '手机/电脑',
      14: '电脑',
      15: '手机/电脑',
    }
    return labels[n] || (isNaN(n) ? '' : '考试方式 ' + n)
  }
  function normalizeExam(item, course, userId) {
    if (!item || typeof item !== 'object') return null
    const idRaw = firstDefined(item, ['examID', 'examId', 'id'])
    const title = String(firstDefined(item, ['title', 'examName', 'name']) || '未命名考试').trim()
    if (idRaw === undefined && title === '未命名考试') return null
    const rawRelations = firstDefined(item, ['relationArr', 'relations', 'batches'])
    const rows = Array.isArray(rawRelations) ? rawRelations : (rawRelations && typeof rawRelations === 'object' ? [rawRelations] : [])
    const relations = rows.map(function (row, index) {
      if (!row || typeof row !== 'object') return null
      const startMs = toMs(firstDefined(row, ['startDate', 'startTime', 'beginTime', 'start']))
      const endMs = toMs(firstDefined(row, ['endDate', 'endTime', 'finishTime', 'end']))
      return { index: index, startMs: startMs, endMs: endMs }
    }).filter(function (row) {
      return row && (row.startMs || row.endMs)
    }).sort(function (a, b) {
      return (a.startMs || a.endMs) - (b.startMs || b.endMs)
    })
    if (!relations.length) {
      const startMs = toMs(firstDefined(item, ['startDate', 'startTime', 'beginTime', 'start']))
      const endMs = toMs(firstDefined(item, ['endDate', 'endTime', 'finishTime', 'end']))
      if (startMs || endMs) relations.push({ index: 0, startMs: startMs, endMs: endMs })
    }
    const statusRaw = firstDefined(item, ['status', 'examStatus', 'state'])
    const status = parseInt(statusRaw, 10)
    return {
      key: 'exam|' + (idRaw !== undefined ? String(idRaw) : title) + '|' + course.id,
      id: idRaw !== undefined ? String(idRaw) : '',
      title: title,
      courseId: course.id,
      courseName: course.name,
      teacher: course.teacher,
      userId: String(userId || ''),
      examType: firstDefined(item, ['examType', 'type']),
      status: isNaN(status) ? null : status,
      hasRecord: !!(item.hasRecord === true || item.hasRecord === 1 || item.hasRecord === '1'),
      examUserID: firstDefined(item, ['examUserID', 'examUserId']) || '',
      showGrade: !!(item.showGrade === true || item.showGrade === 1 || item.showGrade === '1'),
      grade: firstDefined(item, ['grade', 'score']),
      relations: relations,
    }
  }
  function examPhase(item, now) {
    now = now || Date.now()
    const relations = item && Array.isArray(item.relations) ? item.relations : []
    for (let i = 0; i < relations.length; i++) {
      const r = relations[i]
      if (r.startMs && r.endMs && r.startMs <= now && now <= r.endMs) {
        return { phase: 'active', relation: r, total: relations.length }
      }
    }
    for (let i = 0; i < relations.length; i++) {
      const r = relations[i]
      if (r.startMs && r.startMs > now && item.status !== 3 && item.status !== 4) {
        return { phase: 'upcoming', relation: r, total: relations.length }
      }
    }
    for (let i = relations.length - 1; i >= 0; i--) {
      const r = relations[i]
      if ((r.endMs && r.endMs < now) || (!r.endMs && r.startMs && r.startMs < now)) {
        return { phase: 'ended', relation: r, total: relations.length }
      }
    }
    if (item.status === 2 || item.status === 5) return { phase: 'active', relation: relations[0] || null, total: relations.length }
    if (item.status === 1) return { phase: 'upcoming', relation: relations[0] || null, total: relations.length }
    if (item.status === 3 || item.status === 4) return { phase: 'ended', relation: relations[relations.length - 1] || null, total: relations.length }
    return { phase: 'unknown', relation: relations[0] || null, total: relations.length }
  }
  function examStatusLabel(item, now) {
    const p = examPhase(item, now)
    if (p.phase === 'active') return '考试中'
    if (p.phase === 'upcoming') return '待考试'
    if (p.phase === 'ended') return '已结束'
    return '状态未知'
  }
  function examCountdownText(item, now) {
    now = now || Date.now()
    const p = examPhase(item, now)
    const r = p.relation
    if (p.phase === 'upcoming' && r && r.startMs) {
      const left = r.startMs - now
      if (left > 7 * 86400000) return '距开考 ' + Math.ceil(left / 86400000) + '天'
      if (left > 86400000) {
        const d = Math.floor(left / 86400000)
        const h = Math.floor((left % 86400000) / 3600000)
        return '距开考 ' + d + '天' + h + '小时'
      }
      return '距开考 ' + fmtExamClock(left)
    }
    if (p.phase === 'active' && r && r.endMs) {
      const left = r.endMs - now
      if (left <= 0) return '即将结束'
      if (left < 86400000) return '考试中 · 剩 ' + fmtExamClock(left)
      const d = Math.floor(left / 86400000)
      const h = Math.floor((left % 86400000) / 3600000)
      return '考试中 · 剩 ' + d + '天' + h + '小时'
    }
    if (p.phase === 'active') return '考试中'
    if (p.phase === 'ended') return '已结束'
    return '暂无时间'
  }
  function sortExams(items) {
    const now = Date.now()
    const rank = { active: 0, upcoming: 1, ended: 2, unknown: 3 }
    return items.sort(function (a, b) {
      const pa = examPhase(a, now)
      const pb = examPhase(b, now)
      const ra = rank[pa.phase]
      const rb = rank[pb.phase]
      if (ra !== rb) return ra - rb
      const ar = pa.relation || {}
      const br = pb.relation || {}
      if (pa.phase === 'ended') {
        return (br.endMs || br.startMs || 0) - (ar.endMs || ar.startMs || 0)
      }
      const av = ar.startMs || ar.endMs || Number.MAX_SAFE_INTEGER
      const bv = br.startMs || br.endMs || Number.MAX_SAFE_INTEGER
      if (av !== bv) return av - bv
      return a.courseName.localeCompare(b.courseName, 'zh')
    })
  }
  async function collectExams(courses, userId) {
    if (!userId) {
      return {
        items: [],
        failures: [],
        error: '考试列表需要读取当前用户编号，已跳过考试查询；作业汇总不受影响。请重新登录 lms.dgut.edu.cn 后刷新。',
      }
    }
    const results = await Promise.all(courses.map(async function (course) {
      try {
        return { course: course, list: await fetchExams(course, userId) }
      } catch (e) {
        return { course: course, error: e }
      }
    }))
    const items = []
    const failures = []
    let authFailed = 0
    results.forEach(function (result) {
      if (result.error) {
        failures.push(result.course.name + '：' + result.error.message)
        if (result.error.auth) authFailed++
        return
      }
      const seen = new Set()
      result.list.forEach(function (raw) {
        const exam = normalizeExam(raw, result.course, userId)
        if (!exam || seen.has(exam.key)) return
        seen.add(exam.key)
        items.push(exam)
      })
    })
    let error = ''
    if (failures.length) {
      error = authFailed === failures.length
        ? '考试接口登录已失效；作业数据可能仍可用，请重新登录后刷新。'
        : failures.length + ' 门课的考试读取失败：' + preview(failures[0], 120)
    }
    return { items: sortExams(items), failures: failures, error: error }
  }

  // ==================== 刷新 ====================
  function persist() {
    saveJSON(CACHE_KEY, {
      items: state.items,
      exams: state.exams,
      courses: state.courses,
      courseCount: state.courseCount,
      examError: state.examError,
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
        state.exams = []
        state.failures = []
        state.examFailures = []
        state.examError = ''
        state.updatedAt = Date.now()
        state.error = '没有读到课程列表。请确认已登录、且本学期已选课。'
        persist()
        log('课程列表为空（未登录或本学期无课程）', 'err')
        return
      }
      const userId = currentUserId()
      state.userId = userId
      const [results, examBundle] = await Promise.all([
        Promise.all(courses.map(async function (c) {
          try {
            return { course: c, list: await fetchHomeworks(c) }
          } catch (e) {
            return { course: c, error: e }
          }
        })),
        collectExams(courses, userId),
      ])
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
      state.exams = examBundle.items
      state.failures = failures
      state.examFailures = examBundle.failures
      state.examError = examBundle.error
      state.ignored = ignoredStates.size
      state.updatedAt = Date.now()
      persist()
      const todo = countKind('todo')
      const review = countKind('review')
      const hid = hiddenCount()
      const exams = countExamsVisible()
      log('读取 ' + courses.length + ' 门课，待提交 ' + todo + ' · 待互评 ' + review + ' · 考试 ' + exams +
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
      if (examBundle.error) log(examBundle.error, 'err')
      if (examBundle.failures.length && authFailed !== failures.length) {
        log(examBundle.failures.length + ' 门课的考试读取失败：' + preview(examBundle.failures[0], 120), 'err')
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
  function isExamVisible(item) {
    if (hiddenKeys.has(item.key)) return false
    if (!cfg.examShowEnded && examPhase(item).phase === 'ended') return false
    return true
  }
  function countExamsVisible() {
    let n = 0
    for (let i = 0; i < state.exams.length; i++) {
      if (isExamVisible(state.exams[i])) n++
    }
    return n
  }
  function hiddenCount() {
    const homework = state.items.filter(function (i) { return !isVisible(i) }).length
    const exams = state.exams.filter(function (i) {
      return hiddenKeys.has(i.key) || (!cfg.examShowEnded && examPhase(i).phase === 'ended')
    }).length
    return homework + exams
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
      '.tab.exam.on{background:#251b12;border-color:#d29922;color:#f2cc60}',
      '.opts{display:flex;gap:12px;flex-wrap:wrap;color:#7d8590;font-size:11px}',
      '.opts.hidden{display:none}',
      '.opts label{display:flex;align-items:center;gap:4px;cursor:pointer}',
      '.row{border:1px solid #333742;border-radius:6px;padding:6px 8px;background:#191b21;cursor:pointer}',
      '.row:hover{border-color:#4a5060}',
      '.row.exam-active{border-color:#bb8009;background:#211b12}',
      '.row.exam-soon{border-color:#6e5a20}',
      '.r1{display:flex;gap:6px;align-items:baseline}',
      '.badge{flex:0 0 auto;padding:0 5px;border-radius:3px;font-size:10px;font-weight:600}',
      '.badge.todo{background:#4a3a12;color:#e3b341}',
      '.badge.review{background:#1f2a4a;color:#79b8ff}',
      '.badge.exam{background:#25314b;color:#a5d6ff}',
      '.badge.exam.active{background:#5a3500;color:#ffd279}',
      '.badge.exam.ended{background:#2c3038;color:#8b949e}',
      '.ttl{flex:1;word-break:break-all;color:#e6e6e6}',
      '.cd{flex:0 0 auto;color:#d29922;white-space:nowrap;font-weight:600}',
      '.cd.over{color:#ff7b72}',
      '.cd.none{color:#6e7681;font-weight:400}',
      '.cd.exam-active{color:#ffa657}',
      '.cd.exam-soon{color:#f2cc60}',
      '.cd.exam-ended{color:#6e7681;font-weight:400}',
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
      '.exam-summary{border:1px solid #5c491c;border-radius:7px;padding:8px;background:#211b12;color:#e6e6e6;line-height:1.6}',
      '.exam-summary b{color:#f2cc60}',
      '.exam-summary .mini{color:#a78b4a}',
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
      '<div class="hd" id="hwaHd"><b>作业与考试</b><button id="hwaBtnRefresh" title="重新拉取全部课程作业和考试">刷新</button><button id="hwaBtnMin" title="折叠">–</button></div>',
      '<div class="bd">',
      '  <div class="status" id="hwaStatus">加载中…</div>',
      '  <div class="tabs">',
      '    <button class="tab" data-tab="todo" id="hwaTabTodo">待提交 <span id="hwaCntTodo">0</span></button>',
      '    <button class="tab" data-tab="review" id="hwaTabReview">待互评 <span id="hwaCntReview">0</span></button>',
      '    <button class="tab exam" data-tab="exam" id="hwaTabExam">考试 <span id="hwaCntExam">0</span></button>',
      '  </div>',
      '  <div class="opts" id="hwaHwOpts1">',
      '    <label><input type="checkbox" id="hwaShowExpired"> 显示已过期</label>',
      '    <label><input type="checkbox" id="hwaAuto"> 每 10 分钟自动刷新</label>',
      '    <button class="tab" id="hwaBtnUnhide" title="恢复所有手动隐藏的条目">恢复隐藏 (0)</button>',
      '  </div>',
      '  <div class="opts" id="hwaHwOpts2">',
      '    <label class="grow"><span>排除关键词</span><input type="text" id="hwaExclude" placeholder="标题含这些词就不显示，逗号分隔"></label>',
      '    <label title="只显示这么多天内截止的作业，留空或 0 表示不限"><input type="number" id="hwaDays" min="0" step="1" placeholder="不限"> 天内截止</label>',
      '  </div>',
      '  <div class="opts hidden" id="hwaExamOpts">',
      '    <label><input type="checkbox" id="hwaExamShowEnded"> 显示已结束考试</label>',
      '    <span class="mini">临考 24 小时内按秒倒计时</span>',
      '    <button class="tab" id="hwaExamBtnUnhide" title="恢复所有手动隐藏的条目">恢复隐藏 (0)</button>',
      '  </div>',
      '  <div id="hwaExamSummary"></div>',
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
      cntTodo: $('hwaCntTodo'), cntReview: $('hwaCntReview'), cntExam: $('hwaCntExam'),
      tabTodo: $('hwaTabTodo'), tabReview: $('hwaTabReview'), tabExam: $('hwaTabExam'),
      showExpired: $('hwaShowExpired'), auto: $('hwaAuto'),
      exclude: $('hwaExclude'), unhide: $('hwaBtnUnhide'), examUnhide: $('hwaExamBtnUnhide'), days: $('hwaDays'),
      hwOpts1: $('hwaHwOpts1'), hwOpts2: $('hwaHwOpts2'), examOpts: $('hwaExamOpts'),
      examShowEnded: $('hwaExamShowEnded'), examSummary: $('hwaExamSummary'),
    }

    ui.showExpired.checked = cfg.showExpired !== false
    ui.auto.checked = cfg.autoRefresh !== false
    ui.showExpired.addEventListener('change', function () {
      cfg.showExpired = ui.showExpired.checked
      saveJSON(CFG_KEY, cfg)
      renderList()
    })
    ui.examShowEnded.checked = cfg.examShowEnded === true
    ui.examShowEnded.addEventListener('change', function () {
      cfg.examShowEnded = ui.examShowEnded.checked
      saveJSON(CFG_KEY, cfg)
      renderAll()
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
    const restoreHidden = function () {
      if (!hiddenKeys.size) return
      hiddenKeys.clear()
      saveHidden()
      log('已恢复全部隐藏的条目', 'ok')
      renderAll()
    }
    ui.unhide.addEventListener('click', restoreHidden)
    ui.examUnhide.addEventListener('click', restoreHidden)

    const switchTab = function (tab) {
      state.tab = tab
      renderAll()
    }
    ui.tabTodo.addEventListener('click', function () { switchTab('todo') })
    ui.tabReview.addEventListener('click', function () { switchTab('review') })
    ui.tabExam.addEventListener('click', function () { switchTab('exam') })
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

    log('已就绪。点击「刷新」拉取全部课程的作业和考试。', 'ok')
    if (state.updatedAt) {
      log('已加载缓存（' + fmtDateTime(state.updatedAt) + '），作业 ' + state.items.length +
        ' 条，考试 ' + state.exams.length + ' 场', '')
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
    } else if (state.tab === 'exam' && state.examError) {
      s = state.examError
      ui.status.style.color = '#d29922'
    } else if (state.updatedAt) {
      s = state.courseCount + ' 门课 · 待提交 ' + countKind('todo') + ' · 待互评 ' + countKind('review') +
        ' · 考试 ' + countExamsVisible() + ' · ' + fmtClock(state.updatedAt) + ' 更新'
      ui.status.style.color = '#7ee787'
    } else {
      s = '尚未加载'
      ui.status.style.color = '#e6e6e6'
    }
    ui.status.textContent = s
    ui.cntTodo.textContent = countKind('todo')
    ui.cntReview.textContent = countKind('review')
    ui.cntExam.textContent = countExamsVisible()
    ui.tabTodo.classList.toggle('on', state.tab === 'todo')
    ui.tabReview.classList.toggle('on', state.tab === 'review')
    ui.tabExam.classList.toggle('on', state.tab === 'exam')
    ui.hwOpts1.classList.toggle('hidden', state.tab === 'exam')
    ui.hwOpts2.classList.toggle('hidden', state.tab === 'exam')
    ui.examOpts.classList.toggle('hidden', state.tab !== 'exam')
    ui.unhide.textContent = '恢复隐藏 (' + hiddenKeys.size + ')'
    ui.unhide.disabled = hiddenKeys.size === 0
    ui.examUnhide.textContent = '恢复隐藏 (' + hiddenKeys.size + ')'
    ui.examUnhide.disabled = hiddenKeys.size === 0
  }

  function renderList() {
    if (!ui) return
    countdownNodes = []
    ui.list.innerHTML = ''
    ui.examSummary.innerHTML = ''
    if (state.tab === 'exam') {
      renderExamList()
      return
    }
    renderHomeworkList()
  }

  function renderHomeworkList() {
    const now = Date.now()
    const showExpired = cfg.showExpired !== false
    const items = state.items.filter(function (it) {
      if (it.kind !== state.tab) return false
      if (!isVisible(it)) return false
      if (!showExpired && it.endMs && it.endMs < now) return false
      return true
    })
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

  function renderExamList() {
    const now = Date.now()
    const visible = sortExams(state.exams.filter(function (exam) {
      return isExamVisible(exam)
    }))
    const active = visible.find(function (exam) { return examPhase(exam, now).phase === 'active' })
    const upcoming = visible.find(function (exam) { return examPhase(exam, now).phase === 'upcoming' })
    const focus = active || upcoming
    if (focus) {
      const box = document.createElement('div')
      box.className = 'exam-summary'
      const title = document.createElement('b')
      title.textContent = active ? '正在考试 · ' : '下一场 · '
      title.appendChild(document.createTextNode(focus.title))
      const line = document.createElement('div')
      const phase = examPhase(focus, now)
      const relation = phase.relation || {}
      line.textContent = focus.courseName + (relation.startMs ? ' · ' + fmtDateTime(relation.startMs) + ' 开考' : '') + ' · '
      const live = document.createElement('span')
      live.textContent = examCountdownText(focus, now)
      live.style.color = active ? '#ffa657' : '#f2cc60'
      live.style.fontWeight = '600'
      line.appendChild(live)
      countdownNodes.push({ node: live, item: focus, kind: 'exam', lockClass: true })
      const hint = document.createElement('div')
      hint.className = 'mini'
      hint.textContent = visible.filter(function (exam) { return examPhase(exam, now).phase === 'upcoming' }).length +
        ' 场待考，' + visible.filter(function (exam) { return examPhase(exam, now).phase === 'active' }).length + ' 场进行中'
      box.appendChild(title)
      box.appendChild(line)
      box.appendChild(hint)
      ui.examSummary.appendChild(box)
    }
    if (!state.exams.length) {
      const d = document.createElement('div')
      d.className = 'empty'
      d.innerHTML = state.updatedAt
        ? '当前没有考试数据<br><span class="mini">若考试端确实已发布考试，请重新登录后刷新</span>'
        : '还没有数据，点右上角「刷新」'
      ui.list.appendChild(d)
    } else if (!visible.length) {
      const d = document.createElement('div')
      d.className = 'empty'
      d.innerHTML = '当前没有待考或进行中的考试<br><span class="mini">已结束考试默认隐藏，可勾选上方「显示已结束考试」</span>'
      ui.list.appendChild(d)
    }
    visible.forEach(function (exam) {
      const phase = examPhase(exam, now)
      const relation = phase.relation || {}
      const row = document.createElement('div')
      row.className = 'row' +
        (phase.phase === 'active' ? ' exam-active' : '') +
        (phase.phase === 'upcoming' && relation.startMs && relation.startMs - now <= 86400000 ? ' exam-soon' : '')
      const r1 = document.createElement('div')
      r1.className = 'r1'
      const badge = document.createElement('span')
      badge.className = 'badge exam' +
        (phase.phase === 'active' ? ' active' : '') +
        (phase.phase === 'ended' ? ' ended' : '')
      badge.textContent = examStatusLabel(exam, now)
      const ttl = document.createElement('span')
      ttl.className = 'ttl'
      ttl.textContent = exam.title
      const cd = document.createElement('span')
      const soon = phase.phase === 'upcoming' && relation.startMs && relation.startMs - now <= 86400000
      cd.className = 'cd' +
        (phase.phase === 'active' ? ' exam-active' : '') +
        (phase.phase === 'ended' ? ' exam-ended' : '') +
        (soon ? ' exam-soon' : '') +
        (phase.phase === 'unknown' ? ' none' : '')
      cd.textContent = examCountdownText(exam, now)
      const hb = document.createElement('button')
      hb.className = 'hbtn'
      hb.textContent = '×'
      hb.title = '不再显示这场考试'
      hb.addEventListener('click', function (e) {
        e.stopPropagation()
        hiddenKeys.add(exam.key)
        saveHidden()
        log('已隐藏考试：' + preview(exam.title, 40) + '（点「恢复隐藏」可找回）', '')
        renderAll()
      })
      r1.appendChild(badge)
      r1.appendChild(ttl)
      r1.appendChild(cd)
      r1.appendChild(hb)
      const r2 = document.createElement('div')
      r2.className = 'r2'
      const timeText = relation.startMs
        ? fmtDateTime(relation.startMs) + (relation.endMs ? ' - ' + fmtDateTime(relation.endMs) : '')
        : '时间待定'
      r2.textContent = exam.courseName + (exam.teacher ? ' · ' + exam.teacher : '') + ' · ' + timeText
      const meta = []
      const typeText = examTypeLabel(exam.examType)
      if (typeText) meta.push(typeText)
      if (phase.total > 1) meta.push('共 ' + phase.total + ' 场')
      if (exam.showGrade && exam.grade !== undefined && exam.grade !== null) meta.push('成绩 ' + exam.grade)
      if (meta.length) {
        const r3 = document.createElement('div')
        r3.className = 'r2'
        r3.textContent = meta.join(' · ')
        row.appendChild(r1)
        row.appendChild(r2)
        row.appendChild(r3)
      } else {
        row.appendChild(r1)
        row.appendChild(r2)
      }
      row.title = exam.courseId ? '点击在新标签打开该课程的考试页' : '考试信息加载不完整'
      if (exam.courseId) {
        row.addEventListener('click', function () {
          window.open(COURSE_EXAM_PAGE(exam.courseId), '_blank')
        })
      }
      ui.list.appendChild(row)
      countdownNodes.push({ node: cd, item: exam, kind: 'exam' })
    })
    if (state.examFailures.length) {
      const f = document.createElement('div')
      f.className = 'fail'
      f.textContent = '⚠ ' + state.examFailures.length + ' 门课的考试读取失败：' + preview(state.examFailures[0], 100)
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
  let examPhaseSig = ''
  let lastHomeworkTick = 0
  setInterval(function () {
    if (!ui) return
    const now = Date.now()
    const nextSig = state.exams.map(function (exam) {
      return exam.key + ':' + examPhase(exam, now).phase
    }).sort().join('|')
    if (examPhaseSig && nextSig !== examPhaseSig) {
      examPhaseSig = nextSig
      if (state.tab === 'exam') renderAll()
      else renderStatus()
      return
    }
    examPhaseSig = nextSig
    const updateHomework = now - lastHomeworkTick >= 30000
    if (updateHomework) lastHomeworkTick = now
    countdownNodes.forEach(function (n) {
      if (!n.node.isConnected) return
      if (n.kind === 'exam') {
        const txt = examCountdownText(n.item, now)
        if (n.node.textContent !== txt) n.node.textContent = txt
        return
      }
      if (!updateHomework) return
      const txt = countdownText(n.item)
      if (n.node.textContent !== txt) n.node.textContent = txt
      const expired = !!(n.item.endMs && n.item.endMs < now)
      const want = 'cd' + (expired ? ' over' : (n.item.endMs ? '' : ' none'))
      if (n.node.className !== want) n.node.className = want
    })
  }, 1000)

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
      exams: function () { return state.exams.slice() },
      courses: function () { return state.courses.slice() },
      counts: function () {
        return {
          todo: countKind('todo'),
          review: countKind('review'),
          exam: countExamsVisible(),
          totalHomework: state.items.length,
          totalExams: state.exams.length,
        }
      },
      pickList: pickList,
      toMs: toMs,
      normalize: normalizeItem,
      normalizeExam: normalizeExam,
      examPhase: examPhase,
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
      examsRaw: function (courseId) {
        const c = state.courses.find(function (x) { return String(x.id) === String(courseId) })
        const userId = currentUserId()
        if (!c) { console.log('[作业汇总] 没有这门课，先 refresh()'); return Promise.resolve(null) }
        if (!userId) { console.log('[作业汇总] 没有读到用户编号，先登录并 refresh()'); return Promise.resolve(null) }
        return fetchExams(c, userId)
      },
      userId: currentUserId,
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
