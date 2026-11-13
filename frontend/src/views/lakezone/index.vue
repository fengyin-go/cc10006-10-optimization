<template>
  <section class="page" data-module="lakezone">
    <header class="page-head">
      <div>
        <h2>湖区分区管理</h2>
        <p class="page-desc">
          维护湖区分区方案。同一分区范围只保留一个生效策略：先比业务优先级，再比生效时间；
          调整后待归档分区按新口径重算，已归档分区保持原路径。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportEffective">导出当前生效方案</button>
        <button class="btn" type="button" @click="exportRows">导出分区方案清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="notice" class="ok-text">{{ notice }}</p>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">策略冲突与最终裁决</h3>
        <span v-if="decision" class="panel-note">
          裁决基准日 {{ decision.as_of }} · 规则：{{ decision.basis.join(' → ') }}
        </span>
      </div>
      <p v-if="decision" class="decision-note">{{ decision.decision_note }}</p>

      <table class="data-table">
        <thead>
          <tr>
            <th>方案编号</th>
            <th>策略类型</th>
            <th>作用范围</th>
            <th>业务优先级</th>
            <th>生效时间</th>
            <th>保留天数</th>
            <th>阈值</th>
            <th>判定规则</th>
            <th>裁决</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in decision?.candidates ?? []" :key="item['方案编号']">
            <td>{{ item['方案编号'] }}</td>
            <td>{{ item['策略类型'] }}</td>
            <td>{{ item['作用范围'] }}</td>
            <td>{{ item['业务优先级'] }}</td>
            <td>{{ item['生效时间'] }}</td>
            <td>{{ item['保留天数'] }}</td>
            <td>{{ item['阈值'] }}</td>
            <td>{{ item['判定规则'] }}</td>
            <td>
              <span v-if="item['是否生效']" class="winner-badge">唯一生效</span>
              <div v-else>
                <span class="loser-badge">未采纳</span>
                <ul class="conflict-list">
                  <li v-for="point in item['冲突点']" :key="point">{{ point }}</li>
                </ul>
              </div>
            </td>
          </tr>
          <tr v-if="!decision?.candidates?.length">
            <td colspan="9" class="empty-state">当前分区范围没有可用策略</td>
          </tr>
        </tbody>
      </table>

      <div v-if="decision?.rule" class="panel-note" style="margin-top: 8px">
        <span class="tag">最终方案 {{ decision.winner?.['方案编号'] }} {{ decision.rule['版本'] }}</span>
        <span class="tag">保留 {{ decision.rule['保留天数'] }} 天</span>
        <span class="tag">{{ decision.rule['阈值'] }}</span>
        <span class="tag">{{ decision.rule['判定规则'] }}</span>
        <span class="tag">冷热分层：{{ decision.rule['冷热分层'] }}</span>
        <span class="tag">归档路径：{{ decision.rule['归档路径'] }}</span>
      </div>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">调整保留天数</h3>
        <span class="panel-note">上限 {{ retentionLimit }} 天；超出范围会直接拒绝，不会落盘</span>
      </div>
      <form class="inline-form" @submit.prevent="adjustRetention">
        <label>
          <span>目标方案</span>
          <select v-model="adjustForm.policyId">
            <option v-for="item in enabledPolicies" :key="String(item.id)" :value="item.id">
              {{ item['方案编号'] }}（{{ item['策略类型'] }}）
            </option>
          </select>
        </label>
        <label>
          <span>保留天数</span>
          <input v-model="adjustForm.retention" type="number" min="1" />
        </label>
        <button class="btn primary" type="submit">按新口径重算</button>
        <button class="btn" type="button" @click="runConcurrent">并发修改演练</button>
        <button class="btn" type="button" @click="rollbackPolicy">回滚上一版</button>
      </form>
      <p v-if="recalc" class="panel-note">
        最近一次重算：{{ recalc.time }} · 方案 {{ recalc.summary['方案'] }} {{ recalc.summary['版本'] }} ·
        待归档 {{ recalc.summary['待归档分区数'] }} 个，保留 {{ recalc.summary['保留分区数'] }} 个，
        已归档 {{ recalc.summary['已归档分区数'] }} 个保持原路径。
        <span v-for="change in recalc.summary['判定发生变化'] ?? []" :key="change['分区']" class="tag">
          {{ change['分区'] }}：{{ change['原判定'] }} → {{ change['新判定'] }}
        </span>
      </p>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">待归档分区（按最终方案重算）</h3>
        <span class="panel-note">
          {{ partitionSummary }} · 已归档的 {{ archiveGuardCount }} 个分区按归档当时策略保留
        </span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>分区</th>
            <th>距今天数</th>
            <th>文件数</th>
            <th>当前判定</th>
            <th>采用方案</th>
            <th>保留天数</th>
            <th>阈值</th>
            <th>判定规则</th>
            <th>归档路径</th>
            <th>处理说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in partitions" :key="String(row.id)">
            <td>{{ row['分区'] }}</td>
            <td>{{ row['距今天数'] }}</td>
            <td>{{ row['文件数'] }}</td>
            <td :class="row['是否已归档'] ? 'muted' : row['是否待归档'] ? 'miss' : 'hit'">
              {{ row['当前判定'] }}
            </td>
            <td>{{ row['当前方案'] ?? '—' }}</td>
            <td>{{ row['当前保留天数'] ?? '—' }}</td>
            <td>{{ row['当前阈值'] ?? '—' }}</td>
            <td>{{ row['当前判定规则'] ?? '—' }}</td>
            <td>{{ row['归档路径'] ?? '—' }}</td>
            <td>{{ row['处理说明'] ?? '—' }}</td>
          </tr>
          <tr v-if="!partitions.length">
            <td colspan="10" class="empty-state">暂无分区数据</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">小文件合并验证：dt=2025-02-01</h3>
        <span class="panel-note">
          合并只作用于最终方案选中的文件集合（{{ decision?.rule?.['阈值'] }}）；未命中文件（含历史清单）保留在清单中
        </span>
      </div>
      <button class="btn primary" type="button" @click="mergeTarget">按最终方案执行合并</button>
      <table class="data-table" style="margin-top: 8px">
        <thead>
          <tr>
            <th>文件编号</th>
            <th>文件名</th>
            <th>大小MB</th>
            <th>是否历史清单</th>
            <th>处理状态</th>
            <th>命中最终方案</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="file in files" :key="String(file.id)">
            <td>{{ file['文件编号'] }}</td>
            <td>{{ file['文件名'] }}</td>
            <td>{{ file['大小MB'] }}</td>
            <td>{{ file['是否历史清单'] ? '是' : '否' }}</td>
            <td>{{ file['处理状态'] }}</td>
            <td :class="file['命中最终方案'] ? 'hit' : 'miss'">
              {{ file['命中最终方案'] ? '已命中' : '未命中' }}
            </td>
            <td>{{ file['未命中原因'] ?? file['备注'] ?? '—' }}</td>
          </tr>
          <tr v-if="!files.length">
            <td colspan="7" class="empty-state">该分区没有文件清单</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">跨模块同步快照待办</h3>
        <span class="panel-note">策略调整、并发裁决与回滚都会自动登记，版本快照页读取同一份清单</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>待办编号</th>
            <th>类型</th>
            <th>关联方案</th>
            <th>方案版本</th>
            <th>摘要</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="String(todo['待办编号'])">
            <td>{{ todo['待办编号'] }}</td>
            <td>{{ todo['待办类型'] }}</td>
            <td>{{ todo['关联方案'] }}</td>
            <td>{{ todo['方案版本'] }}</td>
            <td>{{ todo['摘要'] }}</td>
            <td>{{ todo['状态'] }}</td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="6" class="empty-state">暂无同步待办</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">裁决与操作留痕</h3>
        <span class="panel-note">每次调整、重算、合并、回滚都可追溯到最终方案与被拒绝的提交</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>事件类型</th>
            <th>方案编号</th>
            <th>说明</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="event in events" :key="String(event['事件编号'])">
            <td>{{ event['时间'] }}</td>
            <td>{{ event['事件类型'] }}</td>
            <td>{{ event['方案编号'] }}</td>
            <td>{{ event['说明'] }}</td>
            <td>{{ event['结果'] }}</td>
          </tr>
          <tr v-if="!events.length">
            <td colspan="5" class="empty-state">暂无事件</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article v-if="rejections.length" class="panel">
      <div class="panel-head"><h3 class="panel-title">被拒绝的保留天数</h3></div>
      <ul class="conflict-list">
        <li v-for="item in rejections" :key="item['修订编号']">
          {{ item['修订编号'] }} · {{ item['方案编号'] }} 提交保留 {{ item['保留天数'] }} 天：{{ item['校验失败'] }}
        </li>
      </ul>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">分区方案清单</h3>
        <span class="panel-note">共 {{ total }} 条，状态流转与策略调整互不影响</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">
              {{ row[column] ?? '—' }}
            </td>
            <td>{{ row.status }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 2" class="empty-state">暂无分区方案</td>
          </tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Candidate = {
  '方案编号': string
  '策略类型': string
  '作用范围': string
  业务优先级: number
  生效时间: string
  保留天数: number
  阈值: string
  判定规则: string
  是否生效: boolean
  冲突点: string[]
}

type Rule = {
  保留天数: number
  阈值: string
  判定规则: string
  冷热分层: string
  归档路径: string
  版本: string
}

type Decision = {
  scope: string
  as_of: string
  basis: string[]
  decision_note: string
  candidates: Candidate[]
  conflict_points: { 方案编号: string; 策略类型: string; 冲突点: string[] }[]
  winner: Candidate | null
  rule: Rule | null
}

type Recalc = {
  time: string
  summary: {
    方案: string
    版本: string
    保留天数: number
    待归档分区数: number
    保留分区数: number
    已归档分区数: number
    判定发生变化: { 分区: string; 原判定: string; 新判定: string }[]
  }
}

const ENDPOINT = '/api/lakezone'
const columns = ['方案编号', '目标表名', '分区字段', '分区粒度', '保留天数', '冷热分层', '归档路径', '小文件合并']
const actions = ['提交校验', '启用方案', '停用方案']
const retentionLimit = 3650
const MERGE_PARTITION_ID = 1

const rows = ref<Record<string, string | number | null>[]>([])
const total = ref(0)
const decision = ref<Decision | null>(null)
const partitions = ref<Record<string, string | number | boolean | null>[]>([])
const files = ref<Record<string, string | number | boolean | null>[]>([])
const todos = ref<Record<string, string | number | null>[]>([])
const events = ref<Record<string, string | number | null>[]>([])
const revisions = ref<Record<string, string | number | null>[]>([])
const recalc = ref<Recalc | null>(null)
const policies = ref<Record<string, string | number>[]>([])
const enabledPolicies = computed(() => policies.value.filter((item) => item.status === '已启用'))
const errorMessage = ref('')
const notice = ref('')
const adjustForm = ref<{ policyId: number; retention: number }>({ policyId: 2, retention: 90 })

const pendingCount = computed(() => partitions.value.filter((row) => row['是否待归档']).length)
const keptCount = computed(
  () => partitions.value.filter((row) => !row['是否已归档'] && !row['是否待归档']).length,
)
const archiveGuardCount = computed(() => partitions.value.filter((row) => row['是否已归档']).length)
const partitionSummary = computed(() => `待归档 ${pendingCount.value} 个 · 保留 ${keptCount.value} 个`)

const stats = computed(() => [
  { label: '分区方案总数', value: policies.value.length },
  { label: '冲突策略', value: decision.value?.conflict_points.length ?? 0 },
  { label: '待归档分区', value: pendingCount.value },
  { label: '已归档分区', value: archiveGuardCount.value },
])

const rejections = computed(() =>
  revisions.value.filter((item) => item['校验失败']).map((item) => ({
    修订编号: String(item['修订编号']),
    方案编号: String(item['方案编号']),
    保留天数: item['保留天数'],
    校验失败: String(item['校验失败']),
  })),
)

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

async function post(path: string, body: unknown) {
  const response = await request(`${ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as { ok: boolean; message: string; entry?: Record<string, unknown> }
}

async function reload() {
  errorMessage.value = ''
  try {
    const [list, current, partitionData, filesData, todoData, eventData, revisionData, policyData] =
      await Promise.all([
        getJson<{ items: typeof rows.value; total: number }>(`${ENDPOINT}?size=200`),
        getJson<Decision>(`${ENDPOINT}/effective`),
        getJson<{ summary: Recalc['summary']; partitions: typeof partitions.value; last_recalc?: Recalc }>(
          `${ENDPOINT}/partitions`,
        ),
        getJson<{ items: typeof files.value }>(`${ENDPOINT}/partitions/files?partition=dt=2025-02-01`),
        getJson<{ items: typeof todos.value }>(`${ENDPOINT}/todos`),
        getJson<{ items: typeof events.value }>(`${ENDPOINT}/events`),
        getJson<{ items: typeof revisions.value }>(`${ENDPOINT}/revisions`),
        getJson<{ items: typeof policies.value }>(`${ENDPOINT}?size=200`),
      ])
    rows.value = list.items
    total.value = list.total
    decision.value = current
    partitions.value = partitionData.partitions
    recalc.value = partitionData.last_recalc ?? null
    files.value = filesData.items
    todos.value = todoData.items
    events.value = [...eventData.items].reverse()
    revisions.value = revisionData.items
    policies.value = policyData.items as unknown as Record<string, string | number>[]
    const selectable = policyData.items.filter((item) => item.status === '已启用')
    if (!selectable.some((item) => Number(item.id) === adjustForm.value.policyId) && selectable.length) {
      adjustForm.value.policyId = Number(selectable[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖区分区数据读取失败'
  }
}

async function adjustRetention() {
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await post('/adjust', {
      values: { id: adjustForm.value.policyId, values: { 保留天数: Number(adjustForm.value.retention) } },
    })
    if (!payload.ok) {
      errorMessage.value = payload.message
    } else {
      notice.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '策略调整失败'
  }
}

async function runConcurrent() {
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await post('/concurrent', {
      values: {
        submissions: [
          { id: 3, values: { 业务优先级: 120, 生效时间: '2026-08-01T00:00:00', 保留天数: 20 } },
          { id: 1, values: { 业务优先级: 120, 生效时间: '2026-09-01T00:00:00', 保留天数: 120 } },
        ],
      },
    })
    notice.value = payload.message
    if (!payload.ok) {
      errorMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '并发裁决失败'
  }
}

async function rollbackPolicy() {
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await post('/rollback', { values: {} })
    notice.value = payload.message
    if (!payload.ok) {
      errorMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '策略回滚失败'
  }
}

async function mergeTarget() {
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await post(`/partitions/${MERGE_PARTITION_ID}/merge`, {})
    notice.value = payload.message
    if (!payload.ok) {
      errorMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '小文件合并失败'
  }
}

async function runAction(action: string, row: Record<string, string | number | null>) {
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await post(`/${row.id}/actions`, { action })
    if (!payload.ok) {
      errorMessage.value = payload.message
    } else {
      notice.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖区分区操作失败'
  }
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function exportEffective() {
  // 只导出当前唯一生效方案，供归档团队复核
  window.open(`${ENDPOINT}/export/effective`, '_blank')
}

onMounted(reload)
</script>
