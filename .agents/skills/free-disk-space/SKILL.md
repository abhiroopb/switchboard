---
name: free-disk-space
description: Survey where disk space goes on a macOS, Windows or Linux developer machine, sort what you find into safe, review and never-touch groups, and delete only the items the user approves one by one. Use when the disk is full or nearly full, when builds fail for lack of space, or when the user asks to reclaim storage.
---

# Freeing disk space

Developer machines fill up with caches, build output, container images and old toolchains. Most of it can be rebuilt. Some of it cannot. Measure first, then let the user choose.

## When to use

- The disk is full or close to it.
- A build, install or container pull fails with "no space left on device".
- The user asks what is using their storage.

## Steps

1. Check free space.

   ```bash
   df -h                                       # macOS and Linux
   ```

   ```powershell
   Get-PSDrive -PSProvider FileSystem          # Windows
   ```

2. Find the largest folders under the home directory, two levels deep.

   ```bash
   du -h -d 2 ~ 2>/dev/null | sort -h | tail -30          # macOS
   du -h --max-depth=2 ~ 2>/dev/null | sort -h | tail -30 # Linux
   ```

   ```powershell
   Get-ChildItem $HOME -Directory -Force -ErrorAction SilentlyContinue |
     ForEach-Object { [pscustomobject]@{ Path = $_.FullName; GB = [math]::Round(((Get-ChildItem $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB), 2) } } |
     Sort-Object GB -Descending | Select-Object -First 20
   ```

3. Measure known heavy spots that exist on this machine. Common ones:
   - Package caches: npm, pnpm, yarn, pip, Homebrew, Cargo, Go modules, NuGet, Maven, Gradle.
   - Build output: `node_modules`, `target`, `build`, `dist`, `.next`, Xcode DerivedData.
   - Containers: `docker system df`.
   - Old SDKs and toolchains: simulator runtimes, Android system images, unused language versions.
   - Trash or Recycle Bin, and the Downloads folder.

4. Sort every finding into three groups and show them to the user as a table with path, size and reason.
   - Safe: rebuilds on demand with no loss. Package manager caches, build output in projects not in use, dangling container images.
   - Review: probably removable but the user must judge. Old SDKs, stopped containers and their volumes, large files in Downloads, the trash, old virtual machines.
   - Never touch: system folders, application data, keychains, mail, photo libraries, source code with uncommitted work, cloud sync folders, databases, anything you cannot explain.

5. Ask the user to approve items one by one. "Clean everything safe" counts as approval only for the items listed in the Safe group at that moment.

6. Prefer each tool's own clean command over deleting folders by hand.

   ```bash
   npm cache clean --force
   pip cache purge
   brew cleanup
   docker image prune
   go clean -modcache
   ```

   Run commands that remove volumes or all images, such as `docker system prune -a --volumes`, only when the user names them.

7. After cleaning, check free space again and report what was recovered per item.

## Safety rules

- Delete nothing without the user's approval for that item in this conversation.
- Never touch the never-touch group, even if the user approves a broad rule. Ask about each one by name.
- Before removing build output or `node_modules`, check the project has no uncommitted work you might confuse with it.
- Do not empty the trash or clear Downloads in bulk. List what is there and let the user choose.
- Do not use elevated privileges unless the user asks and the item requires it.

## Done when

- Free space was measured before and after.
- Every large item was placed in safe, review or never-touch with its size.
- Only approved items were removed, using tool commands where they exist.
- The user has a short report of space recovered and what remains.
