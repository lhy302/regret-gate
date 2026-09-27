# sources/ —— 第三方源码说明

> 建立时间：2026/09/27

---

## 1. 这里有什么

| 目录 | 状态 | 说明 |
|---|---|---|
| `ik_llama.cpp/` | ✅ **已入库** | 推理框架源码，**本项目要魔改其 KV 缓存层**，因此随仓库分发 |
| `llama.cpp/` | ❌ 未入库 | 仅作上游 API 演进对照的参考，需要时自行 clone |

---

## 2. ⚖️ 许可证与署名（重要）

### ik_llama.cpp 是 MIT 许可

完整许可见 [`ik_llama.cpp/LICENSE`](./ik_llama.cpp/LICENSE)。**三个版权持有者：**

```
MIT License

Copyright (c) 2023-2024 The ggml authors
Copyright (c) 2023-2024 The llama.cpp authors
Copyright (c) 2024-2025 The ik_llama.cpp authors
```

### 我们遵守了什么

MIT 许可的核心义务是这一句：

> **"The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software."**
> （版权声明与许可声明须包含在本软件的所有副本或实质性部分中。）

因此**把源码纳入本仓库时，`LICENSE` 与 `AUTHORS` 两个文件必须一并保留** —— 它们已在
`sources/ik_llama.cpp/` 下了。

**⚠️ 未来注意**：

- **不要删除** `sources/ik_llama.cpp/LICENSE` 或 `AUTHORS`，即使你在大量修改源码。
- **不要**把 `ik_llama.cpp/` 里的代码复制到本仓库其它位置而不同时带上许可声明。
- **不要**把本项目的 MIT `LICENSE`（仓库根）与 `ik_llama.cpp` 的 MIT `LICENSE`
  混为一谈 —— 二者都是 MIT，但**版权持有者不同**：
  - 仓库根 `LICENSE` → `Copyright (c) 2026 lhy302`（本项目）
  - `sources/ik_llama.cpp/LICENSE` → 上述三个上游持有者
- 若将来把魔改后的 ik_llama.cpp **单独分发**，同样要带上这三个版权声明。
- 若你魔改后**对外发布**，建议在发布物里注明「基于 ik_llama.cpp（MIT）修改」。

### 关于 MIT 与 MIT 的兼容性

本项目是 MIT，ik_llama.cpp 也是 MIT。**MIT 代码可以放进 MIT 项目**，
只要保留原版权声明即可（即上面的做法）。无需更换本项目的许可证。

---

## 3. 为什么排除了这些文件

`ik_llama.cpp/` 原树 143.6 MB，入库前做了以下排除，最终 **34.15 MB / 2020 文件**：

| 排除项 | 体积 | 为什么 |
|---|---|---|
| `.git/` | 34.13 MB | 上游 git 历史，不影响编译；版本由 commit hash 记录 |
| `models/` | 47.16 MB | 词表 gguf **测试数据**，与源码无关 |
| `github-data/` | 11.93 MB | 抓取的 issue 数据 |
| `examples/server/webui/dist/`、`public_llamacpp/` | 15.39 MB | 网页 UI 构建产物 |
| `media/` | 0.87 MB | 演示图片 |
| `*.gguf` | — | 模型/词表二进制 |

**保留了什么**：完整可编译的源码树（`src/` `include/` `ggml/` `common/` `examples/`
`gguf-py/` `cmake/` `CMakeLists.txt` 等），以及 `LICENSE` + `AUTHORS`。

### 版本锚点

```
ikawrakow/ik_llama.cpp    HEAD 85a3f2c   （浅克隆时的提交）
```

未保留 `.git`，所以**无法在此目录内直接 `git log`**。需要对照上游时：
```powershell
git clone https://github.com/ikawrakow/ik_llama.cpp.git D:\tmp\ik-upstream
cd D:\tmp\ik-upstream; git checkout 85a3f2c
```

---

## 4. 关键改造目标（KV 层）

| 文件 | 作用 |
|---|---|
| `include/llama.h` | 公开 C API —— **KV 操作新方法在此声明** |
| `src/llama-context.h` | `llama_context` 内部结构，KV cache 实例挂在这里 |
| `examples/server/server.cpp` | HTTP 路由与端点注册；`handle_slots_erase` 等是现成范式 |
| `examples/server/server-context.cpp` | 请求处理核心（216.8 KB） |
| `examples/server/server-common.h` | 公共结构体，新增字段放这里 |
| `gguf-py/` | GGUF 读写，`convert_hf_to_gguf.py` 依赖 |
| `CMakeLists.txt` / `cmake/` | 构建（注意：本机 `cmake` 不在 PATH） |

**已存在的 KV 原语**（无需从零实现，见 `include/llama.h` L858–966）：
`llama_kv_cache_seq_rm` / `seq_add` / `seq_div` / `seq_keep` / `seq_cp` /
`defrag` / `update`，以及 `llama_state_seq_get_data` 等状态序列化 API。

详见 [`../docs/协同进化/KV缓存手术 · 工程设计与文献基础 v1.0.md`](../docs/协同进化/KV缓存手术%20·%20工程设计与文献基础%20v1.0.md)。
