import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

async function mockPublicApi(page: Page) {
  await page.route('http://localhost:8000/api/v1/**', async route => {
    const url = new URL(route.request().url());
    let data: unknown = {};

    if (url.pathname.endsWith('/health')) {
      const ok = { status: 'ok' };
      data = {
        status: 'healthy',
        version: '1.1.0',
        uptime_seconds: 60,
        production_ready: true,
        components: {
          document_parser: ok,
          mineru_venv: ok,
          langextract_venv: ok,
          llm_api: ok,
          deepseek_api: ok,
          storage: ok,
          blob_storage: ok,
          graph_database: ok,
          app_database: ok,
          task_queue: { ...ok, durable: true },
        },
      };
    } else if (url.pathname.endsWith('/system/stats')) {
      data = {
        total_documents: 1,
        indexed_documents: 1,
        failed_documents: 0,
        total_nodes: 12,
        total_edges: 8,
        total_queries: 2,
        active_jobs: 0,
        storage_used_mb: 1,
      };
    } else if (url.pathname.endsWith('/documents')) {
      data = {
        total: 1,
        page: 1,
        page_size: 50,
        items: [{
          doc_id: 'doc_1',
          filename: '示例文档.pdf',
          format: 'PDF',
          pages: 3,
          status: 'indexed',
          uploaded_at: '2026-07-15T00:00:00Z',
        }],
      };
    } else if (url.pathname.includes('/query/history') || url.pathname.endsWith('/query/sessions')) {
      data = { total: 0, page: 1, page_size: 50, items: [] };
    } else if (url.pathname.endsWith('/kg')) {
      data = { nodes: [], edges: [], total_nodes: 0, total_edges: 0 };
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 0, msg: 'ok', data }),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await mockPublicApi(page);
});

test('desktop public document workflow is understandable and accessible', async ({ page }, testInfo) => {
  await page.goto('/documents');

  await expect(page.getByText('公开演示 · 可上传并索引')).toBeVisible();
  await expect(page.getByRole('button', { name: '上传文档' })).toBeEnabled();
  await expect(page.getByRole('combobox', { name: '按文档格式筛选' })).toHaveValue('All');
  await expect(page.getByRole('combobox', { name: '按索引状态筛选' })).toHaveValue('All');
  await expect(page.locator('.documents-table').getByText('经典 · 已索引', { exact: true })).toBeVisible();
  await expect(page.getByText('v1.1.0')).toBeVisible();

  const scan = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  await testInfo.attach('axe-results', {
    body: JSON.stringify(scan, null, 2),
    contentType: 'application/json',
  });
  expect(scan.violations).toEqual([]);
});

test('desktop dashboard exposes product-level health labels', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('文档解析', { exact: true })).toBeVisible();
  await expect(page.getByText('实体提取', { exact: true })).toBeVisible();
  await expect(page.getByText('问答与索引', { exact: true })).toBeVisible();
  await expect(page.getByText('文件存储', { exact: true })).toBeVisible();
});
