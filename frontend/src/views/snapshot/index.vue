<template>
  <section class="page" data-module="snapshot">
    <header class="page-head">
      <div>
        <h2>版本快照管理</h2>
        <p class="page-desc">
          维护版本快照记录。“当前生效方案”直接读取湖区分区的裁决结果，不在这里另算一套口径。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出版本快照清单</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">当前生效方案（与湖区分区一致）</h3>
        <span class="panel-note">来源 {{ effective?.source }} · 裁决基准日 {{ effective?.as_of }}</span>
      </div>
      <p v-if="effective" class="decision-note">{{ effective.decision_note }}</p>
      <div v-if="effective?.rule" class="panel-note">
        <span class="tag">分区范围 {{ effective.scope }}</span>
        <span class="tag">方案 {{ effective.winner?.['方案编号'] }} {{ effective.rule['版本'] }}</span>
        <span class="tag">保留 {{ effective.rule['保留天数'] }} 天</span>
        <span class="tag">{{ effective.rule['阈值'] }}</span>
        <span class="tag">{{ effective.rule['判定规则'] }}</span>
      </div>
      <ul v-if="effective?.conflict_points?.length" class="conflict-list">
        <li v-for="item in effective.conflict_points" :key="item['方案编号']">
          {{ item['方案编号'] }}（{{ item['策略类型'] }}）：{{ item['冲突点'].join('；') }}
        </li>
      </ul>
    </article>

    <article class="panel">
      <div class="panel-head">
        <h3 class="panel-title">跨模块同步快照待办</h3>
        <span class="panel-note">来自湖区分区策略裁决，策略调整后自动登记</span>
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
            <th>创建时间</th>
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
            <td>{{ todo['创建时间'] }}</td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="7" class="empty-state">暂无同步待办</td>
          </tr>
        </tbody>
      </table>
    </article>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无版本快照数据，可先登记快照记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条版本快照记录</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type Effective = {
  source: string
  scope: string
  as_of: string
  decision_note: string
  winner: { '方案编号': string } | null
  rule: { 保留天数: number; 阈值: string; 判定规则: string; 版本: string } | null
  conflict_points: { '方案编号': string; '策略类型': string; 冲突点: string[] }[]
}

const ENDPOINT = '/api/snapshot'
const columns = ['快照编号', '目标表名', '快照时间', '快照大小', '存储介质', '过期策略', '恢复校验', '依赖快照']
const actions = ['提交创建', '生效快照', '标记过期']
const stats = [
  { label: '快照记录总数', value: 0 },
  { label: '已生效快照', value: 0 },
  { label: '待创建快照', value: 0 },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const effective = ref<Effective | null>(null)
const todos = ref<Row[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('版本快照动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本快照操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [list, plan, todoData] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/effective`),
      request(`${ENDPOINT}/todos`),
    ])
    if (!list.ok || !plan.ok || !todoData.ok) {
      throw new Error('快照记录或生效方案读取失败')
    }
    const payload = await list.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    effective.value = (await plan.json()) as Effective
    todos.value = ((await todoData.json()) as { items: Row[] }).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本快照列表读取失败'
  }
}

onMounted(reload)
</script>
