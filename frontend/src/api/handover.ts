/** 财务对账交接接口：受理、停住、更正、领取、明细文件与回执。 */
import { request } from './client'

export type HandoverRow = Record<string, string | number | null>

export type HandoverLineage = {
  region: string
  fetch_batch_id: string
  sequence: number
}

export type HandoverBatch = {
  batch_id: string
  sequence: number
  query_id: number | null
  acceptance_no: string
  version: number
  region: string
  amount: number | string | null
  confirmed: boolean
  confirmation: string
  source: string
  created_at: string
}

export type HandoverVersionInfo = {
  version: number
  handover_no: string
  status: string
  complete: boolean
  claimed: boolean
  pending_regions: string[]
}

export type HandoverReceipt = {
  receipt_no: string
  acceptance_no: string
  handover_no: string
  version: number
  fields: string[]
  row_count: number
  rows: HandoverRow[]
  total_amount: number | null
  batch_ids: string[]
  batches: HandoverBatch[]
  lineage: HandoverLineage[]
  confirmed: boolean
  confirmation: string
  detail_sha256: string
  issued_at: string
}

export type HandoverView = {
  ok: boolean
  task_id: number
  acceptance_no: string
  handover_no: string
  version: number
  status: string
  fields: string[]
  query_id: number | null
  declared_rows: HandoverRow[]
  written_rows: HandoverRow[]
  pending_rows: HandoverRow[]
  pending_regions: string[]
  written_regions: string[]
  complete: boolean
  claimable: boolean
  confirmed: boolean
  lineage: HandoverLineage[]
  fetch_batches: HandoverBatch[]
  receipt: HandoverReceipt | null
  receipt_no?: string | null
  claimed: boolean
  versions: HandoverVersionInfo[]
  duplicate?: boolean
  payload_matches_existing?: boolean | null
  message: string
}

export type HandoverSummary = HandoverView & { created_at: string }

export type ReconciliationRegion = {
  region: string
  declared_amount: number | string | null
  received_amount: number | string | null
  difference: number | null
  fetch_batch_id: string | null
  fetch_sequence: number | null
  status: string
}

export type Reconciliation = {
  ok: boolean
  acceptance_no: string
  version: number
  status: string
  claimed: boolean
  claimable: boolean
  receipt_no: string | null
  original_version: { version: number; status: string; continued: boolean; revoked: boolean }
  superseded_by: { version: number; status: string } | null
  continued: boolean
  revoked: boolean
  new_acceptance_required: boolean
  batches: HandoverBatch[]
  batch_ids: string[]
  regions: ReconciliationRegion[]
  declared_total: number | null
  received_total: number | null
  total_matches: boolean
  conclusion: string
}

export type QueryResults = {
  ok: boolean
  query_id: number
  query_name: string
  confirmed: boolean
  available: boolean
  fields: string[]
  default_fields: string[]
  rows: HandoverRow[]
  message: string
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  const payload = (await response.json().catch(() => null)) as
    | (T & { message?: string })
    | null
  if (!response.ok || !payload) {
    throw new Error(payload?.message || fallback)
  }
  return payload as T
}

export async function loadQueryResults(queryId: number): Promise<QueryResults> {
  const response = await request(`/api/data_export/handover/query-results?query_id=${queryId}`)
  return readJson<QueryResults>(response, '指标查询结果读取失败')
}

export async function listHandovers(taskId: number): Promise<HandoverSummary[]> {
  const response = await request(`/api/data_export/handover?task_id=${taskId}`)
  const payload = await readJson<{ items: HandoverSummary[] }>(response, '交接列表读取失败')
  return payload.items ?? []
}

export async function createHandover(body: {
  task_id: number
  acceptance_no?: string
  query_id?: number
  fields: string[]
  rows: HandoverRow[]
  stop_after?: string | null
}): Promise<HandoverView> {
  const response = await request('/api/data_export/handover', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return readJson<HandoverView>(response, '受理单登记失败')
}

export async function correctHandover(
  acceptanceNo: string,
  body: { task_id: number; region: string; amount: number },
): Promise<HandoverView> {
  const response = await request(`/api/data_export/handover/${acceptanceNo}/corrections`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return readJson<HandoverView>(response, '更正提交失败')
}

export async function claimHandover(
  acceptanceNo: string,
  body: { task_id: number; version?: number },
): Promise<HandoverView> {
  const response = await request(`/api/data_export/handover/${acceptanceNo}/claim`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return readJson<HandoverView>(response, '交接领取失败')
}

export async function loadHandover(
  acceptanceNo: string,
  taskId: number,
  version?: number,
): Promise<HandoverView> {
  const query = new URLSearchParams({ task_id: String(taskId) })
  if (version !== undefined) query.set('version', String(version))
  const response = await request(`/api/data_export/handover/${acceptanceNo}?${query}`)
  return readJson<HandoverView>(response, '交接详情读取失败')
}

export async function loadReconciliation(
  acceptanceNo: string,
  taskId: number,
  version?: number,
): Promise<Reconciliation> {
  const query = new URLSearchParams({ task_id: String(taskId) })
  if (version !== undefined) query.set('version', String(version))
  const response = await request(
    `/api/data_export/handover/${acceptanceNo}/reconciliation?${query}`,
  )
  return readJson<Reconciliation>(response, '对账结论读取失败')
}

export async function loadReceipt(
  acceptanceNo: string,
  taskId: number,
  version?: number,
): Promise<{ receipt: HandoverReceipt }> {
  const query = new URLSearchParams({ task_id: String(taskId) })
  if (version !== undefined) query.set('version', String(version))
  const response = await request(`/api/data_export/handover/${acceptanceNo}/receipt?${query}`)
  return readJson<{ receipt: HandoverReceipt }>(response, '回执读取失败')
}

export function detailFileUrl(acceptanceNo: string, taskId: number, version?: number): string {
  const query = new URLSearchParams({ task_id: String(taskId) })
  if (version !== undefined) query.set('version', String(version))
  return `/api/data_export/handover/${acceptanceNo}/detail.csv?${query}`
}
