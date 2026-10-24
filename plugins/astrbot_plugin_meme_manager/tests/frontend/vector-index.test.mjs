import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/a_manage/script.js", import.meta.url),
  "utf8",
);
const operation = source.slice(
  source.indexOf("  async function performVectorRebuild("),
  source.indexOf("  async function confirmAndRebuildVector("),
);

for (const incremental of [true, false]) {
  test(`${
    incremental ? "incremental update" : "full rebuild"
  } submits the intended force flag and restores controls`, async () => {
    const requests = [];
    const restored = [];
    const increment = {};
    const rebuild = {};
    const status = {
      embedding_provider_ready: true,
      semantic_caption_complete: true,
      task_status: "idle",
    };
    const context = vm.createContext({
      vectorOperations: new Set(),
      managePageActive: true,
      activeManagePackId: "pack",
      packSupportsVectorStatus: () => true,
      latestManagePackVectorStatusId: "pack",
      latestManagePackVectorStatus: status,
      incrementPackVectorsBtn: increment,
      rebuildPackVectorsBtn: rebuild,
      showToast() {},
      setButtonBusy(button) {
        button.disabled = true;
      },
      restoreButton(button) {
        restored.push(button);
      },
      async apiPost(route, body) {
        requests.push({ route, body });
        return {};
      },
      async refreshManagePackSummaries() {},
      async refreshManagePackVectorStatus() {
        return status;
      },
    });
    vm.runInContext(operation, context);
    assert.equal(
      await context.performVectorRebuild("pack", status, incremental),
      true,
    );
    assert.equal(requests[0].route, "semantic/rebuild-index");
    assert.equal(requests[0].body.force, !incremental);
    assert.equal(restored[0], incremental ? increment : rebuild);
    status.semantic_caption_complete = false;
    requests.length = 0;
    assert.equal(
      await context.performVectorRebuild("pack", status, incremental),
      incremental,
    );
    assert.equal(requests.length, incremental ? 1 : 0);
    status.embedding_provider_ready = false;
    assert.equal(
      await context.performVectorRebuild("pack", status, incremental),
      false,
    );
  });
}

const refresh = source.slice(
  source.indexOf("  async function refreshManagePackVectorStatus("),
  source.indexOf("  async function performVectorRebuild("),
);

function statusContext(apiGet) {
  const rendered = [];
  const context = vm.createContext({
    vectorOperations: new Set(),
    managePageActive: true,
    activeManagePackId: "pack-a",
    managePackVectorStatusRequestId: 0,
    latestManagePackVectorStatusId: "",
    latestManagePackVectorStatus: null,
    packSupportsVectorStatus: () => true,
    hideManagePackVectorStatus() {},
    renderManagePackVectorStatus(packId, status, state) {
      rendered.push({ packId, status, state: state || "ready" });
    },
    apiGet,
    console: { warn() {} },
  });
  vm.runInContext(refresh, context);
  return { context, rendered };
}

test("a late response returns its captured status without replacing the selected pack", async () => {
  let resolveFirst;
  const first = new Promise((resolve) => {
    resolveFirst = resolve;
  });
  const { context, rendered } = statusContext(async (_route, body) =>
    body.pack_id === "pack-a" ? first : { pack_id: body.pack_id },
  );
  const request = context.refreshManagePackVectorStatus("pack-a");
  context.activeManagePackId = "pack-b";
  await context.refreshManagePackVectorStatus("pack-b");
  resolveFirst({ pack_id: "pack-a" });
  assert.equal((await request).pack_id, "pack-a");
  assert.equal(context.latestManagePackVectorStatusId, "pack-b");
  assert.equal(rendered.at(-1).packId, "pack-b");
});

test("reading a non-selected pack cannot invalidate the selected pack request", async () => {
  let resolveFirst;
  const first = new Promise((resolve) => {
    resolveFirst = resolve;
  });
  const { context, rendered } = statusContext(async (_route, body) =>
    body.pack_id === "pack-a" ? first : { pack_id: body.pack_id },
  );
  const request = context.refreshManagePackVectorStatus("pack-a");
  assert.equal(
    (await context.refreshManagePackVectorStatus("imported-pack")).pack_id,
    "imported-pack",
  );
  resolveFirst({ pack_id: "pack-a" });
  await request;
  assert.equal(context.latestManagePackVectorStatusId, "pack-a");
  assert.ok(rendered.every(({ packId }) => packId === "pack-a"));
});

for (const failed of [false, true]) {
  test(`a ${
    failed ? "failed" : "completed"
  } update cannot restore the previous pack toolbar after switching`, async () => {
    let complete;
    const pending = new Promise((resolve) => {
      complete = resolve;
    });
    const { context, rendered } = statusContext(async (_route, body) => ({
      pack_id: body.pack_id,
    }));
    const status = {
      embedding_provider_ready: true,
      semantic_caption_complete: true,
      task_status: "idle",
    };
    const restored = [];
    Object.assign(context, {
      incrementPackVectorsBtn: {},
      rebuildPackVectorsBtn: {},
      setButtonBusy() {},
      restoreButton(button) {
        restored.push(button);
      },
      showToast() {},
      async apiPost() {
        await pending;
        if (failed) throw new Error("Embedding failed");
        return {};
      },
      async refreshManagePackSummaries() {},
    });
    vm.runInContext(operation, context);
    const running = context.performVectorRebuild("pack-a", status, true);
    assert.equal(
      await context.performVectorRebuild("pack-a", status, true),
      false,
    );
    context.activeManagePackId = "pack-b";
    await context.refreshManagePackVectorStatus("pack-b");
    complete();
    assert.equal(await running, !failed);
    assert.equal(context.latestManagePackVectorStatusId, "pack-b");
    assert.equal(rendered.at(-1).packId, "pack-b");
    assert.equal(restored.length, 0);
  });
}

test("periodic refresh follows automatic vector progress on the visible current pack and stops on page leave", async () => {
  let tick;
  const listeners = new Map();
  const requested = [];
  let failNext = false;
  let inboxRequests = 0;
  const context = vm.createContext({
    managePageActive: true,
    vectorStatusPollInFlight: false,
    vectorOperations: new Set(),
    console: { warn() {} },
    managePackVectorStatusRequestId: 0,
    activeManagePackId: "pack-a",
    document: { hidden: false },
    collectionReview: { dialog: { open: false }, busy: false },
    window: {
      addEventListener(name, callback) {
        listeners.set(name, callback);
      },
      setInterval(callback, interval) {
        assert.equal(interval, 30000);
        tick = callback;
      },
    },
    async loadCollectionReview() {
      inboxRequests++;
    },
    async refreshManagePackVectorStatus(packId) {
      requested.push(packId);
      if (failNext) {
        failNext = false;
        throw new Error("Temporary status failure");
      }
    },
  });
  vm.runInContext(
    source.slice(
      source.indexOf('  window.addEventListener("pagehide",'),
      source.indexOf("  semanticReviewStats?.addEventListener("),
    ),
    context,
  );
  await tick();
  context.activeManagePackId = "pack-b";
  await tick();
  assert.deepEqual(requested, ["pack-a", "pack-b"]);
  failNext = true;
  await tick();
  assert.equal(context.vectorStatusPollInFlight, false);
  await tick();
  context.vectorOperations.add("pack-b");
  await tick();
  assert.equal(requested.length, 4);
  context.vectorOperations.delete("pack-b");
  context.document.hidden = true;
  await tick();
  assert.equal(requested.length, 4);
  context.document.hidden = false;
  listeners.get("pagehide")();
  await tick();
  assert.equal(requested.length, 4);
  assert.equal(inboxRequests, 5);
  listeners.get("pageshow")();
  await tick();
  assert.equal(requested.length, 5);
});

test("a status response received after page leave cannot update the toolbar", async () => {
  let finish;
  const response = new Promise((resolve) => {
    finish = resolve;
  });
  const { context, rendered } = statusContext(async () => response);
  const pending = context.refreshManagePackVectorStatus("pack-a");
  context.managePageActive = false;
  context.managePackVectorStatusRequestId++;
  finish({ pack_id: "pack-a" });
  assert.equal((await pending).pack_id, "pack-a");
  assert.equal(context.latestManagePackVectorStatusId, "");
  assert.equal(rendered.filter(({ state }) => state === "ready").length, 0);
});
