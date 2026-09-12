const $ = (selector) => document.querySelector(selector);

const els = {
  form: $("#renderForm"),
  script: $("#scriptInput"),
  footage: $("#footageInput"),
  dropZone: $("#dropZone"),
  fileList: $("#fileList"),
  normalize: $("#normalizeButton"),
  clear: $("#clearButton"),
  scriptCount: $("#scriptCount"),
  wordCount: $("#wordCount"),
  parseMessage: $("#parseMessage"),
  normalizedPreview: $("#normalizedPreview"),
  renderButton: $("#renderButton"),
  renderSummary: $("#renderSummary"),
  jobPanel: $("#jobPanel"),
  jobStateTitle: $("#jobStateTitle"),
  jobPercent: $("#jobPercent"),
  progressBar: $("#progressBar"),
  jobPhase: $("#jobPhase"),
  safeToLeave: $("#safeToLeave"),
  jobError: $("#jobError"),
  retryJob: $("#retryJob"),
  resultList: $("#resultList"),
  downloadAll: $("#downloadAll"),
  title: $("#titleInput"),
  subtitle: $("#subtitleInput"),
  cta: $("#ctaInput"),
  watermark: $("#watermarkInput"),
  toast: $("#toast"),
};

let parsed = null;
let normalizeTimer = null;
let pollTimer = null;
let toastTimer = null;
let selectedFiles = [];

const storage = {
  script: "vocabMotion.script.v2",
  job: "vocabMotion.activeJob.v2",
  settings: "vocabMotion.settings.v2",
};

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 2800);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(value > 20 * 1024 * 1024 ? 0 : 1)} MB`;
}

function plural(count, label) {
  return `${count || 0} ${label}`;
}

function saveSettings() {
  const payload = {
    title: els.title.value,
    subtitle: els.subtitle.value,
    cta: els.cta.value,
    watermark: els.watermark.value,
  };
  localStorage.setItem(storage.settings, JSON.stringify(payload));
}

function restoreLocalState() {
  const savedScript = localStorage.getItem(storage.script);
  if (savedScript !== null) els.script.value = savedScript;
  try {
    const settings = JSON.parse(localStorage.getItem(storage.settings) || "{}");
    if (settings.title) els.title.value = settings.title;
    if (settings.subtitle) els.subtitle.value = settings.subtitle;
    if (settings.cta) els.cta.value = settings.cta.replaceAll("👇", "").trim();
    if (typeof settings.watermark === "string") els.watermark.value = settings.watermark;
  } catch (_) {
    // Keep defaults when old local data cannot be read.
  }
}

function renderParsePreview(data) {
  parsed = data;
  els.scriptCount.textContent = plural(data.script_count, "kịch bản");
  els.wordCount.textContent = plural(data.word_count, "từ");
  els.renderSummary.textContent = data.script_count
    ? `Sẽ tạo ${data.script_count} video · ${data.word_count} từ`
    : "Sẵn sàng tạo video";

  if (!els.script.value.trim()) {
    els.parseMessage.textContent = "Mỗi dòng dùng mẫu: Tiếng Việt | English. Lặp lại tiêu đề để tách thành video mới.";
    els.parseMessage.classList.remove("warning");
    els.normalizedPreview.hidden = true;
    return;
  }

  if (!data.script_count) {
    els.parseMessage.textContent = "Chưa nhận ra dòng hợp lệ. Mẫu đúng: Từ chối | Reject | reject";
    els.parseMessage.classList.add("warning");
    els.normalizedPreview.hidden = true;
    return;
  }

  const ignoredNote = data.ignored?.length ? ` · Bỏ qua ${data.ignored.length} dòng không đúng mẫu` : "";
  els.parseMessage.textContent = `Đã tự chuẩn hóa và bỏ cột thứ ba/IPA${ignoredNote}.`;
  els.parseMessage.classList.toggle("warning", Boolean(data.ignored?.length));

  els.normalizedPreview.innerHTML = "";
  data.scripts.slice(0, 12).forEach((script) => {
    const chip = document.createElement("div");
    chip.className = "preview-chip";
    const sample = script.items.slice(0, 3).map((item) => item.en).join(" · ");
    chip.innerHTML = `<b>${script.name} · ${script.items.length} từ</b><span></span>`;
    chip.querySelector("span").textContent = sample;
    els.normalizedPreview.appendChild(chip);
  });
  if (data.scripts.length > 12) {
    const chip = document.createElement("div");
    chip.className = "preview-chip";
    chip.innerHTML = `<b>+ ${data.scripts.length - 12} kịch bản nữa</b><span>Sẽ render trong cùng một lượt</span>`;
    els.normalizedPreview.appendChild(chip);
  }
  els.normalizedPreview.hidden = false;
}

async function normalizeScript({ replace = false } = {}) {
  const script = els.script.value;
  if (!script.trim()) {
    renderParsePreview({ scripts: [], script_count: 0, word_count: 0, normalized: "", ignored: [] });
    return;
  }
  try {
    const response = await fetch("/api/normalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script }),
    });
    if (!response.ok) throw new Error("Không chuẩn hóa được kịch bản");
    const data = await response.json();
    renderParsePreview(data);
    if (replace && data.normalized) {
      els.script.value = data.normalized;
      localStorage.setItem(storage.script, data.normalized);
      showToast("Đã chuẩn hóa toàn bộ kịch bản");
    }
  } catch (error) {
    els.parseMessage.textContent = "Chưa kết nối được máy chủ để chuẩn hóa.";
    els.parseMessage.classList.add("warning");
  }
}

function scheduleNormalize() {
  clearTimeout(normalizeTimer);
  normalizeTimer = setTimeout(() => normalizeScript(), 340);
}

function setSelectedFiles(files) {
  selectedFiles = Array.from(files || []);
  els.fileList.innerHTML = "";
  selectedFiles.forEach((file) => {
    const row = document.createElement("div");
    row.className = "file-item";
    const dot = document.createElement("i");
    const name = document.createElement("b");
    const size = document.createElement("span");
    name.textContent = file.name;
    size.textContent = formatBytes(file.size);
    row.append(dot, name, size);
    els.fileList.appendChild(row);
  });
  els.fileList.hidden = !selectedFiles.length;
}

function setUploadState(active) {
  els.renderButton.disabled = active;
  els.renderButton.querySelector("span").textContent = active ? "Đang tải footage…" : "Tạo toàn bộ video";
}

function beginUploadStatus() {
  els.jobPanel.hidden = false;
  els.jobPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  els.jobStateTitle.textContent = "Đang tải footage…";
  els.jobPercent.textContent = "…";
  els.progressBar.style.width = "34%";
  els.progressBar.classList.add("indeterminate");
  els.jobPhase.textContent = "Giữ trang mở đến khi máy chủ báo đã nhận đủ dữ liệu.";
  els.safeToLeave.hidden = true;
  els.jobError.hidden = true;
  els.retryJob.hidden = true;
  els.resultList.innerHTML = "";
  els.downloadAll.hidden = true;
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function postJson(url, payload, maxAttempts = 3) {
  let lastError;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      let data = {};
      try {
        data = await response.json();
      } catch (_) {
        // A gateway may return a plain-text error page.
      }
      if (response.ok) return data;
      const error = new Error(data.error || "Máy chủ trả về lỗi " + response.status);
      error.retryable = response.status === 408 || response.status === 429 || response.status >= 500;
      throw error;
    } catch (error) {
      lastError = error;
      if (error.retryable === false || attempt === maxAttempts) break;
      await wait(attempt * 900);
    }
  }
  throw lastError || new Error("Không kết nối được máy chủ.");
}

async function createUploadSession() {
  els.jobPhase.textContent = "Đang chuẩn bị phiên tải an toàn…";
  return postJson("/api/uploads", {
    script: els.script.value,
    title: els.title.value,
    subtitle: els.subtitle.value,
    cta: els.cta.value,
    watermark: els.watermark.value,
    files: selectedFiles.map((file) => ({
      name: file.name,
      size: file.size,
      type: file.type,
    })),
  });
}

function updateUploadProgress(loaded, total, fileName) {
  const percent = Math.max(1, Math.min(99, Math.round((loaded / Math.max(1, total)) * 100)));
  els.jobPercent.textContent = percent + "%";
  els.progressBar.classList.remove("indeterminate");
  els.progressBar.style.width = percent + "%";
  els.jobPhase.textContent = "Đang tải " + fileName + " · " + formatBytes(loaded) + " / " + formatBytes(total);
}

function uploadChunkOnce(jobId, fileIndex, offset, blob, baseBytes, totalBytes, fileName) {
  return new Promise((resolve, reject) => {
    const formData = new FormData();
    formData.append("file_index", String(fileIndex));
    formData.append("offset", String(offset));
    formData.append("chunk", blob, "chunk.bin");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads/" + jobId + "/chunk");
    xhr.responseType = "json";
    xhr.timeout = 90000;
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) updateUploadProgress(baseBytes + event.loaded, totalBytes, fileName);
    });
    xhr.addEventListener("load", () => {
      const data = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data);
        return;
      }
      const error = new Error(data.error || "Máy chủ trả về lỗi " + xhr.status);
      error.retryable = xhr.status === 408 || xhr.status === 429 || xhr.status >= 500;
      if (Number.isFinite(Number(data.expected_offset))) error.expectedOffset = Number(data.expected_offset);
      reject(error);
    });
    xhr.addEventListener("error", () => {
      const error = new Error("Kết nối chập chờn khi tải footage.");
      error.retryable = true;
      reject(error);
    });
    xhr.addEventListener("timeout", () => {
      const error = new Error("Tải một phần footage quá thời gian.");
      error.retryable = true;
      reject(error);
    });
    xhr.send(formData);
  });
}

async function uploadChunkWithRetry(jobId, fileIndex, offset, blob, baseBytes, totalBytes, fileName) {
  let lastError;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      return await uploadChunkOnce(jobId, fileIndex, offset, blob, baseBytes, totalBytes, fileName);
    } catch (error) {
      lastError = error;
      if (Number.isFinite(error.expectedOffset)) {
        return { file_received_bytes: error.expectedOffset };
      }
      if (error.retryable === false || attempt === 5) break;
      els.jobPhase.textContent = "Mạng chập chờn · đang thử lại phần " + fileName + " (" + attempt + "/5)…";
      await wait(Math.min(5000, attempt * 1100));
    }
  }
  throw new Error((lastError && lastError.message ? lastError.message : "Không tải được footage") + " Đã tự thử lại 5 lần.");
}

async function uploadAllChunks(session) {
  const chunkSize = Math.max(128 * 1024, Number(session.chunk_size || 768 * 1024));
  const totalBytes = selectedFiles.reduce((sum, file) => sum + file.size, 0);
  let completedBytes = 0;

  for (let fileIndex = 0; fileIndex < selectedFiles.length; fileIndex += 1) {
    const file = selectedFiles[fileIndex];
    let offset = 0;
    while (offset < file.size) {
      const end = Math.min(file.size, offset + chunkSize);
      const blob = file.slice(offset, end);
      const data = await uploadChunkWithRetry(
        session.job_id, fileIndex, offset, blob, completedBytes + offset, totalBytes, file.name
      );
      const received = Number(data.file_received_bytes);
      if (!Number.isFinite(received) || received < 0 || received > file.size) {
        throw new Error("Máy chủ trả về tiến độ footage không hợp lệ.");
      }
      if (received === offset) {
        throw new Error("Máy chủ chưa nhận thêm dữ liệu footage.");
      }
      offset = received;
      updateUploadProgress(completedBytes + offset, totalBytes, file.name);
    }
    completedBytes += file.size;
  }
}

async function uploadJobInChunks() {
  const session = await createUploadSession();
  await uploadAllChunks(session);
  els.jobPhase.textContent = "Đang kiểm tra footage đã tải đủ…";
  return postJson("/api/uploads/" + session.job_id + "/complete", {}, 4);
}

function renderResults(results) {
  els.resultList.innerHTML = "";
  (results || []).forEach((result) => {
    const row = document.createElement("div");
    row.className = "result-item";
    const info = document.createElement("div");
    const name = document.createElement("strong");
    const meta = document.createElement("small");
    const link = document.createElement("a");
    name.textContent = result.name;
    meta.textContent = `Bài ${result.script_index} · ${formatBytes(result.size_bytes)}`;
    link.textContent = "Tải MP4";
    link.href = result.download_url;
    info.append(name, meta);
    row.append(info, link);
    els.resultList.appendChild(row);
  });
}

function renderJobStatus(data) {
  els.jobPanel.hidden = false;
  els.downloadAll.hidden = true;
  els.retryJob.hidden = true;
  const percent = Math.max(0, Math.min(100, Number(data.percent || 0)));
  els.progressBar.classList.remove("indeterminate");
  els.progressBar.style.width = `${percent}%`;
  els.jobPercent.textContent = `${percent}%`;
  els.jobPhase.textContent = data.phase || "Đang xử lý…";
  els.safeToLeave.hidden = false;
  els.jobError.hidden = true;
  renderResults(data.results);

  if (data.state === "completed") {
    els.jobStateTitle.textContent = `Đã xong ${data.results?.length || data.script_count} video`;
    els.downloadAll.href = data.download_all_url;
    els.downloadAll.hidden = false;
    clearTimeout(pollTimer);
    showToast("Đã render xong toàn bộ video");
  } else if (data.state === "failed") {
    els.jobStateTitle.textContent = "Render bị lỗi";
    els.jobError.textContent = data.error || "Có lỗi trong lúc render.";
    els.jobError.hidden = false;
    els.retryJob.dataset.jobId = data.job_id;
    els.retryJob.hidden = false;
    clearTimeout(pollTimer);
  } else if (data.state === "queued") {
    els.jobStateTitle.textContent = "Đang chờ lượt render";
  } else {
    const current = data.current_script ? ` · bài ${data.current_script}/${data.script_count}` : "";
    els.jobStateTitle.textContent = `Đang render${current}`;
  }
}

async function pollJob(jobId, immediate = false) {
  clearTimeout(pollTimer);
  try {
    const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    if (response.status === 404) {
      localStorage.removeItem(storage.job);
      return;
    }
    if (!response.ok) throw new Error("Chưa đọc được tiến độ");
    const data = await response.json();
    renderJobStatus(data);
    if (!["completed", "failed"].includes(data.state)) {
      pollTimer = setTimeout(() => pollJob(jobId), document.hidden ? 12000 : 2200);
    }
  } catch (_) {
    if (immediate) {
      els.jobPanel.hidden = false;
      els.jobStateTitle.textContent = "Đang nối lại tiến độ…";
    }
    pollTimer = setTimeout(() => pollJob(jobId), 6000);
  }
}

els.script.addEventListener("input", () => {
  localStorage.setItem(storage.script, els.script.value);
  scheduleNormalize();
});

els.script.addEventListener("paste", () => setTimeout(scheduleNormalize, 0));

els.normalize.addEventListener("click", () => normalizeScript({ replace: true }));

els.clear.addEventListener("click", () => {
  els.script.value = "";
  localStorage.setItem(storage.script, "");
  renderParsePreview({ scripts: [], script_count: 0, word_count: 0, normalized: "", ignored: [] });
  els.script.focus();
  showToast("Đã xóa kịch bản");
});

els.footage.addEventListener("change", () => setSelectedFiles(els.footage.files));

["dragenter", "dragover"].forEach((type) => els.dropZone.addEventListener(type, (event) => {
  event.preventDefault();
  els.dropZone.classList.add("dragging");
}));

["dragleave", "drop"].forEach((type) => els.dropZone.addEventListener(type, (event) => {
  event.preventDefault();
  els.dropZone.classList.remove("dragging");
}));

els.dropZone.addEventListener("drop", (event) => {
  const files = Array.from(event.dataTransfer.files || []).filter((file) => file.type.startsWith("video/") || /\.(mp4|mov|m4v|webm|mkv)$/i.test(file.name));
  if (!files.length) return showToast("Hãy chọn file video footage");
  const transfer = new DataTransfer();
  files.forEach((file) => transfer.items.add(file));
  els.footage.files = transfer.files;
  setSelectedFiles(files);
});

[els.title, els.subtitle, els.cta, els.watermark].forEach((input) => input.addEventListener("input", saveSettings));

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await normalizeScript();
  if (!parsed?.script_count) {
    els.script.focus();
    return showToast("Kịch bản chưa có dòng từ vựng hợp lệ");
  }
  if (!selectedFiles.length) {
    els.dropZone.scrollIntoView({ behavior: "smooth", block: "center" });
    return showToast("Hãy chọn ít nhất một video footage");
  }

  saveSettings();
  setUploadState(true);
  beginUploadStatus();
  try {
    const data = await uploadJobInChunks();
    localStorage.setItem(storage.job, data.job_id);
    els.safeToLeave.hidden = false;
    showToast("Đã nhận đủ dữ liệu · có thể thoát app");
    pollJob(data.job_id, true);
  } catch (error) {
    els.progressBar.classList.remove("indeterminate");
    els.progressBar.style.width = "0";
    els.jobPercent.textContent = "!";
    els.jobStateTitle.textContent = "Chưa bắt đầu render";
    els.jobPhase.textContent = "Dữ liệu chưa được máy chủ nhận đủ.";
    els.jobError.textContent = error.message;
    els.jobError.hidden = false;
    els.retryJob.hidden = true;
  } finally {
    setUploadState(false);
  }
});

els.retryJob.addEventListener("click", async () => {
  const jobId = els.retryJob.dataset.jobId || localStorage.getItem(storage.job);
  if (!jobId) return;
  els.retryJob.disabled = true;
  els.retryJob.textContent = "Đang khởi động lại…";
  els.jobError.hidden = true;
  els.jobStateTitle.textContent = "Đang thử lại render";
  try {
    const data = await postJson("/api/jobs/" + jobId + "/retry", {}, 3);
    showToast(data.message || "Đã đưa lượt render trở lại hàng đợi");
    pollJob(jobId, true);
  } catch (error) {
    els.jobError.textContent = error.message;
    els.jobError.hidden = false;
  } finally {
    els.retryJob.disabled = false;
    els.retryJob.textContent = "Thử lại render — không cần tải lại footage";
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    const jobId = localStorage.getItem(storage.job);
    if (jobId) pollJob(jobId, true);
  }
});

window.addEventListener("online", () => {
  const jobId = localStorage.getItem(storage.job);
  if (jobId) pollJob(jobId, true);
});

restoreLocalState();
normalizeScript();
const activeJob = localStorage.getItem(storage.job);
if (activeJob) pollJob(activeJob, true);
