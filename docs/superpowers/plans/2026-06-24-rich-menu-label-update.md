# Rich Menu Label Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the repo Rich Menu design deliverables so the third tile is `產業預測`, text is larger, and the lower-left branding text is removed.

**Architecture:** Keep LINE backend behavior simple: only change the Flex preview mapping and documentation. Add one SVG source file under `assets/` for the manual LINE Official Account Manager upload workflow.

**Tech Stack:** Python 3.10, unittest, SVG, Markdown.

---

## File Structure

- Modify `app.py`: change `build_line_navigation_flex()` third entry from `強勢訊號` to `產業預測` with action text `預測`.
- Modify `tests/test_web_product.py`: update Rich Menu mapping test.
- Modify `docs/line-to-web-map.md`: update the third Rich Menu row.
- Create `assets/rich-menu.svg`: six-tile source design with larger labels and no `AI Quant Bot` text.

---

### Task 1: Flex preview and docs

**Files:**
- Modify: `tests/test_web_product.py`
- Modify: `app.py`
- Modify: `docs/line-to-web-map.md`

- [ ] **Step 1: Write the failing test**

Update `test_line_navigation_maps_six_entries_to_web_routes_and_line_actions` expected message map:

```python
self.assertEqual(actual_message, {
    "我的關注": "我的關注",
    "產業預測": "預測",
    "提醒管理": "提醒管理",
    "投資試算": "投資試算",
})
self.assertNotIn("強勢訊號", actual_message)
```

- [ ] **Step 2: Run the focused test**

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path; & 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product.WebProductTests.test_line_navigation_maps_six_entries_to_web_routes_and_line_actions -v
```

Expected: fail because current preview still uses `強勢訊號`.

- [ ] **Step 3: Implement the minimal change**

Change `app.py` entry:

```python
("產業預測", "查看每日產業預測與分類機會", "選擇產業", {"type": "message", "label": "選擇產業", "text": "預測"}),
```

Update `docs/line-to-web-map.md` row from `強勢訊號` to `產業預測`.

- [ ] **Step 4: Re-run focused test**

Same command as Step 2. Expected: pass.

- [ ] **Step 5: Commit**

```powershell
git add app.py tests/test_web_product.py docs/line-to-web-map.md
git commit -m "feat: point rich menu to sector prediction"
```

---

### Task 2: SVG source design

**Files:**
- Create: `assets/rich-menu.svg`

- [ ] **Step 1: Add SVG design source**

Create a 2500x1686 SVG with six tiles:

```text
今日盤勢 / 我的關注 / 產業預測
提醒管理 / 投資試算 / 完整分析
```

Use large 116px labels, smaller 42px hints, and no `AI Quant Bot` text.

- [ ] **Step 2: Verify source content**

```powershell
Select-String -Path assets\rich-menu.svg -Pattern 'AI Quant Bot|強勢訊號'
```

Expected: no output.

```powershell
Select-String -Path assets\rich-menu.svg -Pattern '產業預測'
```

Expected: one matching label.

- [ ] **Step 3: Commit**

```powershell
git add assets/rich-menu.svg
git commit -m "design: add rich menu source"
```

---

### Task 3: Final verification and push

**Files:**
- Verify only.

- [ ] **Step 1: Run focused product tests**

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path; & 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_web_product -v
```

Expected: all tests pass.

- [ ] **Step 2: Run full suite**

```powershell
$env:PYTHONPATH=(Resolve-Path '.deps').Path; & 'C:\Users\enzo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Check diff and push**

```powershell
git diff --check
git push origin main
```

Expected: no diff check output; push updates `main`.
