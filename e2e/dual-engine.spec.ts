import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';


async function mockDualEngineApi(
  page: Page,
  seenQueries: Array<Record<string, unknown>> = [],
  options: { lightragStatus?: 'done' | 'indexing' | 'failed' } = {},
) {
  await page.route('http://localhost:8000/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname.endsWith('/query/stream')) {
      const body = request.postDataJSON();
      seenQueries.push({ ...body, __path: url.pathname });
      if (body.question === 'force LightRAG failure') {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ code: 503, msg: 'LightRAG unavailable' }),
        });
        return;
      }
      const answer = `LightRAG ${body.retrieval_mode ?? 'legacy'} contract verified.`;
      const result = {
        id: 'q_dual_engine',
        question: body.question,
        answer,
        tool_calls: [],
        cited_nodes: [],
        cited_entities: [],
        references: [{
          doc_id: 'fixture_doc_01',
          filename: 'fixture.md',
          page: 1,
          chunk_id: 'chunk_1',
          excerpt: 'Synthetic page-one evidence.',
        }],
        duration_seconds: 0.1,
        timestamp: '2026-07-20T00:00:00Z',
        session_id: 's_dual_engine',
        engine: body.engine,
        retrieval_mode: body.retrieval_mode,
      };
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'event: status\ndata: {"message":"retrieving"}\n\n',
          `event: answer_delta\ndata: ${JSON.stringify({ text: answer })}\n\n`,
          `event: done\ndata: ${JSON.stringify(result)}\n\n`,
        ].join(''),
      });
      return;
    }

    let data: unknown = {};
    if (url.pathname.endsWith('/health')) {
      data = {
        status: 'healthy',
        version: '1.1.0',
        uptime_seconds: 60,
        components: {
          document_parser: { status: 'ok' },
          mineru_venv: { status: 'ok' },
          langextract_venv: { status: 'ok' },
          llm_api: { status: 'ok' },
          storage: { status: 'ok' },
          graph_database: { status: 'ok' },
          lightrag: { status: 'ok' },
        },
      };
    } else if (url.pathname.endsWith('/system/stats')) {
      data = {
        total_documents: 0,
        total_nodes: 0,
        total_edges: 0,
        total_queries: 0,
      };
    } else if (url.pathname.endsWith('/documents')) {
      const lightragStatus = options.lightragStatus ?? 'done';
      data = { total: 1, page: 1, page_size: 50, items: [{
        doc_id: 'fixture_doc_01',
        filename: 'fixture.md',
        format: 'md',
        pages: 1,
        status: 'indexed',
        uploaded_at: '2026-07-20T00:00:00Z',
        job_id: lightragStatus === 'indexing' ? 'job_dual' : undefined,
        progress: lightragStatus === 'indexing' ? { parsed_pages: 1, total_pages: 2 } : undefined,
        indexes: {
          legacy: { status: 'done', job_id: 'job_dual', stats: { nodes: 1, edges: 0, pages: 1 } },
          lightrag: {
            status: lightragStatus,
            job_id: 'job_dual',
            progress: lightragStatus === 'indexing' ? 50 : 100,
            stats: lightragStatus === 'done' ? { nodes: 2, edges: 1, pages: 1 } : undefined,
          },
        },
        available_engines: lightragStatus === 'done' ? ['legacy', 'lightrag'] : ['legacy'],
      }] };
    } else if (url.pathname.includes('/query/history')) {
      data = { total: 0, page: 1, page_size: 50, items: [] };
    } else if (url.pathname.endsWith('/query/sessions')) {
      data = { total: 0, page: 1, page_size: 50, items: [] };
    } else if (url.pathname.endsWith('/kg/nodes')) {
      const engine = url.searchParams.get('engine') ?? 'legacy';
      data = { total: 2, page: 1, page_size: 200, items: [
        {
          id: `${engine}:node_1`, name: `${engine} node one`, type: 'CONCEPT', source_doc: 'fixture_doc_01',
          page: 1, pages: [1], degree: 1, engine,
        },
        {
          id: `${engine}:node_2`, name: `${engine} node two`, type: 'TECHNOLOGY', source_doc: 'fixture_doc_01',
          page: 1, pages: [1], degree: 1, engine,
        },
      ] };
    } else if (url.pathname.endsWith('/kg/edges')) {
      const engine = url.searchParams.get('engine') ?? 'legacy';
      data = { total: 1, page: 1, page_size: 5000, items: [{
        id: `${engine}:edge_1`, source: `${engine}:node_1`, target: `${engine}:node_2`,
        relation: 'related', description: 'fixture relation', weight: 0.8, pages: [1], engine,
      }] };
    } else if (url.pathname.endsWith('/kg/stats')) {
      data = { total_nodes: 2, total_edges: 1, density: 1, type_distribution: { CONCEPT: 1, TECHNOLOGY: 1 }, relation_types: { related: 1 }, top5_central_nodes: [], source_documents: ['fixture_doc_01'] };
    } else if (url.pathname.endsWith('/kg/export')) {
      const engine = url.searchParams.get('engine') ?? 'legacy';
      data = {
        format: 'json', doc_id: 'fixture_doc_01', total_nodes: 2, total_edges: 1,
        exported_at: '2026-07-20T00:00:00Z',
        nodes: [
          { id: `${engine}:node_1`, name: `${engine} node one`, type: 'CONCEPT', source_doc: 'fixture_doc_01', page: 1, pages: [1], degree: 1, engine },
          { id: `${engine}:node_2`, name: `${engine} node two`, type: 'TECHNOLOGY', source_doc: 'fixture_doc_01', page: 1, pages: [1], degree: 1, engine },
        ],
        edges: [{ id: `${engine}:edge_1`, source: `${engine}:node_1`, target: `${engine}:node_2`, relation: 'related', description: 'fixture relation', weight: 0.8, pages: [1], engine }],
      };
    } else if (request.method() === 'DELETE' && url.pathname.includes('/index/jobs/')) {
      data = { cancelled: true, job_id: 'job_dual', previous_status: 'indexing' };
    } else if (request.method() === 'POST' && url.pathname.endsWith('/index/fixture_doc_01/retry')) {
      data = { job_id: 'job_retry', doc_id: 'fixture_doc_01', status: 'queued' };
    } else {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ code: 404, msg: `Unhandled E2E route: ${request.method()} ${url.pathname}` }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 0, msg: 'success', data }),
    });
  });
}


test('chat keeps the selected LightRAG mode in the SSE request and result', async ({ page }, testInfo) => {
  await mockDualEngineApi(page);
  await page.goto('/chat');

  const engine = page.getByRole('combobox', { name: '选择问答引擎' });
  const mode = page.getByRole('combobox', { name: '选择 LightRAG 检索模式' });
  await expect(engine).toHaveValue('lightrag');
  await mode.selectOption('hybrid');

  const streamRequest = page.waitForRequest(request =>
    request.url().endsWith('/api/v1/query/stream') && request.method() === 'POST',
  );
  await page.getByPlaceholder('向知识图谱提问...').fill('Verify the dual-engine contract');
  await page.getByRole('button', { name: '发送' }).click();

  const payload = (await streamRequest).postDataJSON();
  expect(payload.engine).toBe('lightrag');
  expect(payload.retrieval_mode).toBe('hybrid');
  await expect(page.getByText('LightRAG hybrid contract verified.')).toBeVisible();

  const scan = await new AxeBuilder({ page })
    .include('.chat-content')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  await testInfo.attach('dual-engine-axe-results', {
    body: JSON.stringify(scan, null, 2),
    contentType: 'application/json',
  });
  expect(scan.violations).toEqual([]);
});

test('all five LightRAG modes remain explicit user choices', async ({ page }) => {
  const seen: Array<Record<string, unknown>> = [];
  await mockDualEngineApi(page, seen);
  await page.goto('/chat');

  const mode = page.getByRole('combobox', { name: '选择 LightRAG 检索模式' });
  for (const value of ['local', 'global', 'hybrid', 'mix', 'naive']) {
    await mode.selectOption(value);
    const request = page.waitForRequest(candidate => candidate.url().endsWith('/api/v1/query/stream'));
    await page.getByPlaceholder('向知识图谱提问...').fill(`question for ${value}`);
    await page.getByRole('button', { name: '发送' }).click();
    expect((await request).postDataJSON().retrieval_mode).toBe(value);
    await expect(page.getByText(`LightRAG ${value} contract verified.`)).toBeVisible();
  }
  expect(seen.map(item => item.retrieval_mode)).toEqual(['local', 'global', 'hybrid', 'mix', 'naive']);
});

test('LightRAG failure is explicit and never silently retried on legacy', async ({ page }) => {
  const seen: Array<Record<string, unknown>> = [];
  await mockDualEngineApi(page, seen);
  await page.goto('/chat');
  await page.getByPlaceholder('向知识图谱提问...').fill('force LightRAG failure');
  await page.getByRole('button', { name: '发送' }).click();

  await expect(page.getByText('LightRAG 当前不可用，系统没有静默切换引擎。')).toBeVisible();
  expect(seen).toHaveLength(1);
  expect(seen[0].engine).toBe('lightrag');
  expect(seen[0].__path).toBe('/api/v1/query/stream');
});

test('document keeps dual status and parent cancellation while LightRAG is active', async ({ page }) => {
  await mockDualEngineApi(page, [], { lightragStatus: 'indexing' });
  await page.goto('/documents');

  await expect(page.getByText('经典 · 已索引')).toBeVisible();
  await expect(page.getByText('LightRAG · 索引中')).toBeVisible();
  const cancel = page.waitForRequest(request =>
    request.method() === 'DELETE' && request.url().endsWith('/api/v1/index/jobs/job_dual'),
  );
  await page.getByRole('button', { name: '取消' }).click();
  await cancel;
});

test('failed LightRAG retry targets only the failed engine endpoint', async ({ page }) => {
  await mockDualEngineApi(page, [], { lightragStatus: 'failed' });
  await page.goto('/documents');

  const retry = page.waitForRequest(request =>
    request.method() === 'POST' && request.url().endsWith('/api/v1/index/fixture_doc_01/retry'),
  );
  await page.getByRole('button', { name: '重试 LightRAG' }).click();
  const request = await retry;
  expect(request.postDataJSON()).toEqual({ engine: 'lightrag' });
});

test('citation page, graph engine URL, relayout, drag and JSON export stay usable', async ({ page }) => {
  await mockDualEngineApi(page);
  await page.goto('/chat');
  await page.getByPlaceholder('向知识图谱提问...').fill('show citation');
  await page.getByRole('button', { name: '发送' }).click();
  await page.getByRole('button', { name: /fixture\.md · 第 1 页/ }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('doc_id')).toBe('fixture_doc_01');
  await expect.poll(() => new URL(page.url()).searchParams.get('engine')).toBe('lightrag');

  const engine = page.getByRole('combobox', { name: '选择图谱引擎' });
  await engine.selectOption('legacy');
  await expect.poll(() => new URL(page.url()).searchParams.get('engine')).toBe('legacy');
  await expect(page.locator('circle[data-node-id="legacy:node_1"]')).toHaveCount(1);
  await engine.selectOption('lightrag');
  await expect.poll(() => new URL(page.url()).searchParams.get('engine')).toBe('lightrag');
  const node = page.locator('circle[data-node-id="lightrag:node_1"]');
  const neighbor = page.locator('circle[data-node-id="lightrag:node_2"]');
  const label = page.locator('text[data-label-node-id="lightrag:node_1"]');
  await expect(node).toHaveCount(1);
  await expect(neighbor).toHaveCount(1);
  await expect(label).toHaveCount(1);
  await page.getByRole('button', { name: '重新计算图谱布局' }).click();

  const before = await node.evaluate(element => ({
    cx: Number(element.getAttribute('cx')),
    cy: Number(element.getAttribute('cy')),
  }));
  const neighborBefore = await neighbor.evaluate(element => ({
    cx: Number(element.getAttribute('cx')),
    cy: Number(element.getAttribute('cy')),
  }));
  const box = await node.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + 45, box!.y + box!.height / 2 + 20, { steps: 6 });
  await page.waitForTimeout(120);
  const whileDragging = await node.evaluate(element => Number(element.getAttribute('cx')));
  expect(Math.abs(whileDragging - before.cx)).toBeGreaterThan(20);
  await page.mouse.up();
  await page.waitForTimeout(250);
  const neighborAfter = await neighbor.evaluate(element => ({
    cx: Number(element.getAttribute('cx')),
    cy: Number(element.getAttribute('cy')),
  }));
  expect(Math.hypot(neighborAfter.cx - neighborBefore.cx, neighborAfter.cy - neighborBefore.cy)).toBeGreaterThan(0.1);
  await page.waitForTimeout(750);
  const settled = await neighbor.evaluate(element => ({
    cx: Number(element.getAttribute('cx')),
    cy: Number(element.getAttribute('cy')),
  }));
  await page.waitForTimeout(250);
  const stopped = await neighbor.evaluate(element => ({
    cx: Number(element.getAttribute('cx')),
    cy: Number(element.getAttribute('cy')),
  }));
  expect(Math.hypot(stopped.cx - settled.cx, stopped.cy - settled.cy)).toBeLessThan(0.5);

  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 JSON' }).click();
  const exported = await download;
  const exportedPath = await exported.path();
  expect(exportedPath).not.toBeNull();
  const payload = JSON.parse(await readFile(exportedPath!, 'utf8'));
  expect(payload.filters.engine).toBe('lightrag');
  expect(payload.stats).toEqual({ nodes: 2, edges: 1 });
});
