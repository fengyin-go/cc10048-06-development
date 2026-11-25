<template>
  <section class="handover" data-testid="handover-panel">
    <header class="handover-head">
      <div>
        <h3>财务对账交接</h3>
        <p class="page-desc">
          选地区与入账额发起交接，每一笔都追到已确认的取数批次；交接件完整后才能领取对账明细。
        </p>
      </div>
      <div class="page-actions">
        <label class="handover-task">
          <span>导出任务</span>
          <select v-model.number="taskId" data-testid="handover-task" @change="refreshList">
            <option v-for="row in tasks" :key="String(row.id)" :value="Number(row.id)">
              #{{ row.id }} {{ row['任务标识'] }}
            </option>
          </select>
        </label>
        <button class="btn" type="button" data-testid="handover-reload" @click="refreshList">
          刷新交接
        </button>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="submitCreate">
      <label>
        <span>查询方案</span>
        <select v-model.number="queryId" data-testid="handover-query" @change="loadResults">
          <option v-for="row in queries" :key="String(row.id)" :value="Number(row.id)">
            #{{ row.id }} {{ row['方案标识'] }}
          </option>
        </select>
      </label>
      <label>
        <span>交接字段</span>
        <span class="field-picks">
          <label v-for="field in availableFields" :key="field" class="field-pick">
            <input
              v-model="selectedFields"
              type="checkbox"
              :value="field"
              :data-testid="`handover-field-${field}`"
            />
            {{ field }}
          </label>
        </span>
      </label>
      <label>
        <span>写到该地区后停住</span>
        <select v-model="stopAfter" data-testid="handover-stop-after">
          <option value="">全部写完</option>
          <option v-for="region in declaredRegions" :key="region" :value="region">{{ region }}</option>
        </select>
      </label>
    </form>

    <table class="data-table handover-table">
      <thead>
        <tr>
          <th>地区</th>
          <th>入账额</th>
          <th v-if="selectedFields.includes('渠道')">渠道</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in draftRows" :key="String(row['地区'])">
          <td>{{ row['地区'] }}</td>
          <td>
            <input
              v-model.number="draftAmounts[String(row['地区'])]"
              type="number"
              step="0.01"
              :data-testid="`handover-amount-${row['地区']}`"
            />
          </td>
          <td v-if="selectedFields.includes('渠道')">{{ row['渠道'] }}</td>
          <td>
            <button class="link" type="button" @click="removeDraft(String(row['地区']))">移除</button>
          </td>
        </tr>
        <tr v-if="!draftRows.length">
          <td :colspan="selectedFields.includes('渠道') ? 4 : 3" class="empty-state">
            请选择查询方案以载入可用结果
          </td>
        </tr>
      </tbody>
    </table>

    <div class="page-actions handover-actions">
      <button class="btn primary" type="button" data-testid="handover-submit" @click="submitCreate">
        登记受理单并发起交接
      </button>
      <span v-if="message" class="handover-message" data-testid="handover-message">{{ message }}</span>
    </div>

    <article v-if="active" class="handover-card" data-testid="handover-detail">
      <header class="handover-card-head">
        <div>
          <strong>受理单 {{ active.acceptance_no }}</strong>
          <span class="handover-chip" :data-status="active.status">{{ active.status }}</span>
          <span class="handover-muted">版本 v{{ active.version }} / {{ active.handover_no }}</span>
        </div>
        <div class="page-actions">
          <button
            class="btn"
            type="button"
            data-testid="handover-claim"
            :disabled="!active.claimable"
            @click="claim()"
          >
            领取交接件
          </button>
          <a
            v-if="active.complete && active.claimed"
            class="btn"
            data-testid="handover-download"
            :href="detailFileUrl(active.acceptance_no, taskId, active.version)"
            target="_blank"
            rel="noopener"
          >
            下载对账明细
          </a>
        </div>
      </header>

      <p class="page-desc">
        交接字段：{{ active.fields.join('、') }}；已写入 {{ active.written_regions.join('、') || '—' }}；
        待写入 {{ active.pending_regions.join('、') || '—' }}
      </p>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="field in active.fields" :key="field">{{ field }}</th>
            <th>取数批次</th>
            <th>批次序号</th>
            <th>金额更正</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="line in active.lineage" :key="line.fetch_batch_id">
            <td v-for="field in active.fields" :key="field">{{ rowValue(line, field) }}</td>
            <td>{{ line.fetch_batch_id }}</td>
            <td>第 {{ line.sequence }} 次</td>
            <td>
              <input
                v-model.number="correctionAmounts[line.region]"
                type="number"
                step="0.01"
                class="handover-inline-input"
                :data-testid="`handover-correct-amount-${line.region}`"
              />
              <button
                class="link"
                type="button"
                :data-testid="`handover-correct-${line.region}`"
                @click="correct(line.region)"
              >
                更正
              </button>
            </td>
          </tr>
          <tr v-for="row in active.pending_rows" :key="`pending-${row['地区']}`">
            <td v-for="field in active.fields" :key="field">{{ row[field] }}</td>
            <td colspan="2" class="handover-muted">尚未写入，可先更正金额再写入</td>
            <td>
              <input
                v-model.number="correctionAmounts[String(row['地区'])]"
                type="number"
                step="0.01"
                class="handover-inline-input"
                :data-testid="`handover-correct-amount-${row['地区']}`"
              />
              <button
                class="link"
                type="button"
                :data-testid="`handover-correct-${row['地区']}`"
                @click="correct(String(row['地区']))"
              >
                更正
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <table class="data-table handover-versions">
        <thead>
          <tr>
            <th>版本</th>
            <th>交接单</th>
            <th>状态</th>
            <th>完整</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in active.versions" :key="item.version">
            <td>v{{ item.version }}</td>
            <td>{{ item.handover_no }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.complete ? '是' : `缺 ${item.pending_regions.join('、')}` }}</td>
          </tr>
        </tbody>
      </table>

      <section v-if="receipt" class="handover-receipt" data-testid="handover-receipt">
        <h4>受理回执 {{ receipt.receipt_no }}</h4>
        <p class="page-desc">
          回执 {{ receipt.confirmation }}；明细摘要 {{ receipt.detail_sha256.slice(0, 16) }}…；
          使用批次 {{ receipt.batch_ids.join('、') }}
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="field in receipt.fields" :key="field">{{ field }}</th>
              <th>取数批次</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="line in receipt.lineage" :key="line.fetch_batch_id">
              <td v-for="field in receipt.fields" :key="field">{{ rowValue(line, field) }}</td>
              <td>{{ line.fetch_batch_id }}</td>
            </tr>
          </tbody>
        </table>
        <p class="handover-muted">合计入账额：{{ receipt.total_amount }}</p>
      </section>

      <section v-if="reconciliation" class="handover-recon" data-testid="handover-recon">
        <h4>对账结论</h4>
        <p>{{ reconciliation.conclusion }}</p>
        <p class="page-desc">
          原单 v{{ reconciliation.original_version.version }}：{{ reconciliation.original_version.status }}；
          是否需另发受理单：{{ reconciliation.new_acceptance_required ? '是' : '否' }}；
          金额合计 {{ reconciliation.received_total }} / 报单 {{ reconciliation.declared_total }}
        </p>
      </section>
    </article>

    <table v-if="handovers.length" class="data-table handover-list">
      <thead>
        <tr>
          <th>受理单</th>
          <th>版本</th>
          <th>状态</th>
          <th>字段</th>
          <th>已写 / 待写</th>
          <th>回执</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="order in handovers" :key="order.acceptance_no">
          <td>{{ order.acceptance_no }}</td>
          <td>v{{ order.version }}</td>
          <td>{{ order.status }}</td>
          <td>{{ order.fields.join('、') }}</td>
          <td>
            {{ order.written_rows.map((row) => row['地区']).join('、') || '—' }}
            /
            {{ order.pending_regions.join('、') || '—' }}
          </td>
          <td>{{ order.receipt?.receipt_no || order.receipt_no || '—' }}</td>
          <td>
            <button class="link" type="button" @click="selectOrder(order.acceptance_no)">查看</button>
          </td>
        </tr>
      </tbody>
    </table>

    <p v-if="errorMessage" class="error-text" data-testid="handover-error">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import {
  claimHandover,
  correctHandover,
  createHandover,
  detailFileUrl,
  listHandovers,
  loadHandover,
  loadQueryResults,
  loadReconciliation,
  type HandoverRow,
  type HandoverSummary,
  type HandoverView,
  type Reconciliation,
} from '@/api/handover'

const tasks = ref<EntryRow[]>([])
const queries = ref<EntryRow[]>([])
const taskId = ref(1)
const queryId = ref(1)
const availableFields = ref<string[]>(['地区', '入账额'])
const selectedFields = ref<string[]>(['地区', '入账额'])
const stopAfter = ref('')
const sourceRows = ref<HandoverRow[]>([])
const draftRows = ref<HandoverRow[]>([])
const draftAmounts = reactive<Record<string, number>>({})
const correctionAmounts = reactive<Record<string, number>>({})
const handovers = ref<HandoverSummary[]>([])
const active = ref<HandoverView | null>(null)
const reconciliation = ref<Reconciliation | null>(null)
const message = ref('')
const errorMessage = ref('')

const declaredRegions = computed(() => sourceRows.value.map((row) => String(row['地区'])))
const receipt = computed(() => active.value?.receipt ?? null)

function rowValue(line: Record<string, unknown>, field: string): unknown {
  const value = line[field]
  return value === undefined || value === null ? '—' : value
}

function reportError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function loadBase() {
  try {
    const [taskPage, queryPage] = await Promise.all([
      listEntries('data_export', {}),
      listEntries('dim_query', {}),
    ])
    tasks.value = taskPage.items
    queries.value = queryPage.items
    if (!tasks.value.some((row) => Number(row.id) === taskId.value) && tasks.value.length) {
      taskId.value = Number(tasks.value[0].id)
    }
    if (!queries.value.some((row) => Number(row.id) === queryId.value) && queries.value.length) {
      queryId.value = Number(queries.value[0].id)
    }
  } catch (error) {
    reportError(error, '导出任务读取失败')
  }
}

async function loadResults() {
  errorMessage.value = ''
  try {
    const payload = await loadQueryResults(queryId.value)
    availableFields.value = payload.fields
    sourceRows.value = payload.rows
    draftRows.value = payload.rows.map((row) => ({ ...row }))
    for (const row of payload.rows) {
      const region = String(row['地区'])
      draftAmounts[region] = Number(row['入账额'] ?? 0)
      correctionAmounts[region] = Number(row['入账额'] ?? 0)
    }
    if (!selectedFields.value.includes('地区')) selectedFields.value = ['地区', '入账额']
  } catch (error) {
    reportError(error, '指标查询结果读取失败')
  }
}

function removeDraft(region: string) {
  draftRows.value = draftRows.value.filter((row) => String(row['地区']) !== region)
  if (stopAfter.value === region) stopAfter.value = ''
}

async function submitCreate() {
  errorMessage.value = ''
  message.value = ''
  if (!selectedFields.value.includes('地区')) {
    errorMessage.value = '交接字段必须包含「地区」'
    return
  }
  const rows = draftRows.value.map((row) => {
    const region = String(row['地区'])
    return { ...row, 入账额: draftAmounts[region] ?? row['入账额'] }
  })
  try {
    const result = await createHandover({
      task_id: taskId.value,
      query_id: queryId.value,
      fields: selectedFields.value,
      rows,
      stop_after: stopAfter.value || null,
    })
    active.value = result
    message.value = result.message
    await refreshList()
  } catch (error) {
    reportError(error, '受理单登记失败')
  }
}

async function claim() {
  if (!active.value) return
  errorMessage.value = ''
  try {
    const result = await claimHandover(active.value.acceptance_no, { task_id: taskId.value })
    active.value = result
    message.value = result.message
    await loadReconciliationView(result.acceptance_no, result.version)
    await refreshList()
  } catch (error) {
    reportError(error, '交接领取失败')
  }
}

async function correct(region: string) {
  if (!active.value) return
  errorMessage.value = ''
  try {
    const result = await correctHandover(active.value.acceptance_no, {
      task_id: taskId.value,
      region,
      amount: Number(correctionAmounts[region]),
    })
    active.value = result
    message.value = result.message
    reconciliation.value = null
    await refreshList()
  } catch (error) {
    reportError(error, '更正提交失败')
  }
}

async function selectOrder(acceptanceNo: string) {
  errorMessage.value = ''
  try {
    const view = await loadHandover(acceptanceNo, taskId.value)
    active.value = view
    reconciliation.value = null
    if (view.claimed) await loadReconciliationView(acceptanceNo, view.version)
  } catch (error) {
    reportError(error, '交接详情读取失败')
  }
}

async function loadReconciliationView(acceptanceNo: string, version?: number) {
  try {
    reconciliation.value = await loadReconciliation(acceptanceNo, taskId.value, version)
  } catch (error) {
    reportError(error, '对账结论读取失败')
  }
}

async function refreshList() {
  errorMessage.value = ''
  try {
    handovers.value = await listHandovers(taskId.value)
  } catch (error) {
    reportError(error, '交接列表读取失败')
  }
}

onMounted(async () => {
  await loadBase()
  await loadResults()
  await refreshList()
})
</script>
