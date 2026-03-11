#!/usr/bin/env python3
"""
Microsoft Teams 国内版（百度渠道）个人账户启用 Patch 脚本

通过修改 enableConsumerTenant 相关逻辑，启用被禁用的
个人（MSA/Consumer）账户登录功能。

原理: 国内版通过 isBaidu()=true → enableConsumerTenant=false 禁用个人账户。
本脚本将 enableConsumerTenant 始终返回/设置为 true。

用法:
    python3 patch_china_teams.py <input.apk> [--output <output.apk>]

依赖:
    sudo apt install apktool default-jdk zipalign apksigner
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


KEYSTORE_FILE = os.environ.get("KEYSTORE_FILE") or "china-patch-key.jks"
KEYSTORE_PASS = os.environ.get("KEYSTORE_PASS") or "chinapatch123"
KEY_ALIAS = os.environ.get("KEY_ALIAS") or "chinapatch"
KEY_PASS = os.environ.get("KEY_PASS") or KEYSTORE_PASS
JAVA_HEAP_SIZE = "6g"


def run_cmd(cmd, check=True, capture=False, java_heap=False):
    """执行外部命令"""
    print(f"  [CMD] {' '.join(cmd)}")
    env = None
    if java_heap:
        env = os.environ.copy()
        env["_JAVA_OPTIONS"] = f"-Xmx{JAVA_HEAP_SIZE}"
    return subprocess.run(
        cmd, check=check, capture_output=capture,
        text=True if capture else None, env=env,
    )


def check_dependencies():
    """检查所需工具是否存在"""
    tools = ["apktool", "zipalign"]
    missing = [t for t in tools if shutil.which(t) is None]

    has_apksigner = shutil.which("apksigner") is not None
    has_jarsigner = shutil.which("jarsigner") is not None
    if not has_apksigner and not has_jarsigner:
        missing.append("apksigner or jarsigner")
    if shutil.which("keytool") is None:
        missing.append("keytool")

    if missing:
        print(f"[ERROR] 缺少以下工具: {', '.join(missing)}")
        print("请安装: sudo apt install apktool default-jdk zipalign apksigner")
        sys.exit(1)

    return has_apksigner


# ====== 主流程步骤 ======

def decompile_apk(apk_path, output_dir):
    """使用 apktool -r 模式反编译 (仅 smali，资源保持二进制)"""
    print(f"\n[1/5] 反编译 APK: {apk_path}")
    run_cmd([
        "apktool", "d",
        "-f",
        "-r",  # 不解码资源
        "-o", output_dir,
        str(apk_path),
    ], java_heap=True)
    print(f"  ✓ smali 反编译完成 → {output_dir}")


def find_smali_file(work_dir, class_path):
    """
    在所有 smali_classesN 目录中查找指定类的 smali 文件。
    class_path: 例如 "com/microsoft/skype/teams/services/configuration/AppConfigurationImpl"
    返回 Path 或 None。
    """
    work = Path(work_dir)
    target = class_path + ".smali"
    for smali_dir in sorted(work.glob("smali*")):
        candidate = smali_dir / target
        if candidate.exists():
            return candidate
    return None


def patch_enable_consumer_tenant(work_dir):
    """
    Patch enableConsumerTenant 相关逻辑，启用个人账户支持。

    修改点:
    1. AppConfigurationImpl.enableConsumerTenant() → 始终返回 true
    2. AuthAppConfiguration.<init> → enableConsumerTenant 字段始终为 true
    3. AppConfigurationImpl.shouldShowSignUpButton() → 移除 isBaidu 限制
    """
    print("\n[2/5] Patch: 启用 Consumer Tenant (个人账户)")

    patch_count = 0

    # === Patch 1: AppConfigurationImpl.enableConsumerTenant() ===
    #
    # 原始逻辑:
    #   return (this.mIsNordenDevice || AppBuildConfigurationHelper.isBaidu()) ? false : true;
    #
    # 目标: 始终返回 true
    #
    # smali 方法签名:
    #   .method public final enableConsumerTenant()Z
    #     ... (各种检查)
    #     return v0
    #   .end method

    app_config_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/configuration/AppConfigurationImpl"
    )

    if app_config_file:
        content = app_config_file.read_text(encoding="utf-8")

        # 替换整个 enableConsumerTenant 方法体为直接返回 true
        pattern = (
            r'(\.method public final enableConsumerTenant\(\)Z)'
            r'.*?'
            r'(\.end method)'
        )
        replacement = (
            r'\1\n'
            '    .locals 1\n'
            '\n'
            '    # [CHINA-PATCH] 始终返回 true，启用个人账户\n'
            '    const/4 v0, 0x1\n'
            '\n'
            '    return v0\n'
            r'\2'
        )
        new_content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
        if new_content != content:
            app_config_file.write_text(new_content, encoding="utf-8")
            patch_count += 1
            print(f"  ✓ AppConfigurationImpl.enableConsumerTenant() → return true")
            print(f"    文件: {app_config_file.relative_to(work_dir)}")
        else:
            print("  [WARN] AppConfigurationImpl: 未找到 enableConsumerTenant 方法")
    else:
        print("  [WARN] 未找到 AppConfigurationImpl.smali")

    # === Patch 2: AuthAppConfiguration.<init> ===
    #
    # 原始逻辑 (构造方法中):
    #   this.enableConsumerTenant = (isNorden() || isBaidu()) ? false : true;
    #
    # smali 模式:
    #   invoke-static {}, ...isBaidu()Z
    #   move-result vN
    #   ... (or/xor 逻辑)
    #   iput-boolean vN, p0, ...AuthAppConfiguration;->enableConsumerTenant:Z
    #
    # 目标: 在 iput-boolean 之前强制设置 vN = 1 (true)

    auth_config_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/configuration/AuthAppConfiguration"
    )

    if auth_config_file:
        content = auth_config_file.read_text(encoding="utf-8")

        # 匹配 enableConsumerTenant 字段赋值位置
        # 找到 iput-boolean vN, p0, ...enableConsumerTenant:Z
        # 在它之前插入 const/4 vN, 0x1
        iput_pattern = re.compile(
            r'(    iput-boolean (v\d+), p0, '
            r'Lcom/microsoft/skype/teams/services/configuration/'
            r'AuthAppConfiguration;->enableConsumerTenant:Z)'
        )

        match = iput_pattern.search(content)
        if match:
            reg = match.group(2)  # e.g., v0
            full_line = match.group(1)
            inject = (
                f'    # [CHINA-PATCH] 强制 enableConsumerTenant = true\n'
                f'    const/4 {reg}, 0x1\n'
                f'\n'
                f'{full_line}'
            )
            new_content = content.replace(full_line, inject, 1)
            if new_content != content:
                auth_config_file.write_text(new_content, encoding="utf-8")
                patch_count += 1
                print(f"  ✓ AuthAppConfiguration.<init>: enableConsumerTenant = true")
                print(f"    文件: {auth_config_file.relative_to(work_dir)}")
        else:
            print("  [WARN] AuthAppConfiguration: 未找到 enableConsumerTenant 字段赋值")
    else:
        print("  [WARN] 未找到 AuthAppConfiguration.smali")

    # === Patch 3: AppConfigurationImpl.shouldShowSignUpButton() ===
    #
    # 原始逻辑:
    #   if (isKingston() || isRealWear() || isPanel()) return false;
    #   if (isBaidu()) return false;            ← 百度版本限制
    #   if (disableTflTenant) return false;
    #   return true;
    #
    # smali 模式:
    #   invoke-static {}, ...AppBuildConfigurationHelper;->isBaidu()Z
    #   move-result v2
    #   if-nez v2, :cond_XX   ← 跳转到 return false
    #
    # 目标: 移除 isBaidu() 导致的条件跳转，允许百度版本显示注册按钮
    #       当登录遇到错误时，用户可以通过注册按钮进行恢复

    if app_config_file and app_config_file.exists():
        content = app_config_file.read_text(encoding="utf-8")

        # 找到 shouldShowSignUpButton 方法
        method_pattern = re.compile(
            r'\.method public final shouldShowSignUpButton\(\)Z'
            r'(.*?)'
            r'\.end method',
            re.DOTALL
        )
        method_match = method_pattern.search(content)
        if method_match:
            method_body = method_match.group(0)
            # 在方法体内找到 isBaidu()Z 后的 if-nez 条件跳转
            baidu_pattern = re.compile(
                r'(invoke-static \{}, L[^;]*AppBuildConfigurationHelper;->isBaidu\(\)Z'
                r'\s*\n\s*move-result (v\d+)\s*\n)'
                r'(\s*if-nez v\d+, :\w+)'
            )
            baidu_match = baidu_pattern.search(method_body)
            if baidu_match:
                old_fragment = baidu_match.group(0)
                # 保留 invoke 和 move-result，将 if-nez 替换为 smali 注释
                new_fragment = (
                    baidu_match.group(1)
                    + '\n    # [CHINA-PATCH] 移除 isBaidu 限制，允许显示注册按钮'
                )
                new_method = method_body.replace(old_fragment, new_fragment, 1)
                new_content = content.replace(method_body, new_method, 1)
                if new_content != content:
                    app_config_file.write_text(new_content, encoding="utf-8")
                    patch_count += 1
                    print(f"  ✓ AppConfigurationImpl.shouldShowSignUpButton() → 移除 isBaidu 限制")
                    print(f"    文件: {app_config_file.relative_to(work_dir)}")
                else:
                    print("  [WARN] shouldShowSignUpButton: 替换未生效")
            else:
                print("  [WARN] shouldShowSignUpButton 方法中未找到 isBaidu 条件跳转")
        else:
            print("  [WARN] 未找到 shouldShowSignUpButton 方法")

    # 注: UserConfiguration 通过 IAppConfiguration.enableConsumerTenant() 间接获取，
    # 已由 Patch 1 覆盖，不需要额外修改

    if patch_count == 0:
        print("  [ERROR] 未能成功应用任何 Consumer Tenant patch!")
        sys.exit(1)
    else:
        print(f"\n  总计成功应用 {patch_count} 处 Consumer Tenant patch")

    return patch_count


def _extract_redirect_uri(work_dir):
    """
    从 smali 中动态提取原始 redirect URI 签名哈希。
    查找模式: const-string vN, "HASH%3D" 后跟 goto :goto_1 和 msauth:// 拼接逻辑。
    返回完整的 redirect URI 字符串，或 None。
    """
    # redirect URI builder 中, 生产环境哈希紧接在 :goto_1 之前
    # 模式:
    #   const-string v0, "fcg80qvoM1YMKJZibjBwQcDfOno%3D"
    #   goto :goto_1
    # 后面 :goto_1 处有 msauth:// 拼接
    hash_pattern = re.compile(
        r'const-string v0, "([A-Za-z0-9+/]+%3D)"\n'
        r'\n'
        r'    goto :goto_1'
    )

    for smali_dir in sorted(Path(work_dir).glob("smali*")):
        for smali_file in smali_dir.rglob("*.smali"):
            try:
                content = smali_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if 'msauth://' not in content or 'getApplicationId' not in content:
                continue
            match = hash_pattern.search(content)
            if match:
                uri_hash = match.group(1)
                uri = f"msauth://com.microsoft.teams/{uri_hash}"
                print(f"  从 smali 提取 redirect URI: {uri}")
                print(f"    来源: {smali_file.relative_to(work_dir)}")
                return uri
    return None


# 回退默认值 (Microsoft 签名证书 SHA1 哈希, 极少变化)
_FALLBACK_REDIRECT_URI = "msauth://com.microsoft.teams/fcg80qvoM1YMKJZibjBwQcDfOno%3D"


def patch_redirect_uri(work_dir):
    """
    修复 MSAL redirect URI:
    1. 硬编码 redirect URI builder 返回原始注册的 URI (让 OAuth 服务器接受)
    2. 绕过 OneAuth 的 redirect URI 本地校验 (避免签名不匹配导致闪退)

    redirect URI 从 smali 中动态提取，确保适配 Teams 版本升级。
    """
    print("\n[*] Patch: 修复 redirect URI (签名变更适配)")

    # 动态提取原始 redirect URI
    redirect_uri = _extract_redirect_uri(work_dir)
    if not redirect_uri:
        redirect_uri = _FALLBACK_REDIRECT_URI
        print(f"  [WARN] 未能从 smali 提取, 使用回退值: {redirect_uri}")

    smali_dirs = sorted(Path(work_dir).glob("smali*"))

    # === Part 1: 硬编码 redirect URI builder ===
    # 动态构造 msauth://pkg/hash 的代码，替换为直接返回原始注册的 URI
    builder_patched = False
    builder_pattern = re.compile(
        r'(    :goto_1\n)'
        r'    invoke-static \{}, Lcom/microsoft/teams/core/utilities/'
        r'AppBuildConfigurationHelper;->getApplicationId\(\)'
        r'Ljava/lang/String;\n'
        r'\n'
        r'    move-result-object v1\n'
        r'\n'
        r'    const-string v2, "msauth://"\n'
        r'\n'
        r'    const-string v3, "/"\n'
        r'\n'
        r'    invoke-static \{v2, v1, v3, v0\}, '
        r'L[^;]+;->m'
        r'\(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;'
        r'Ljava/lang/String;\)Ljava/lang/String;\n'
        r'\n'
        r'    move-result-object v0\n'
        r'\n'
        r'    return-object v0'
    )

    builder_replacement = (
        '    :goto_1\n'
        f'    const-string v0, "{{}}"\n'
        '\n'
        '    return-object v0'
    ).format(redirect_uri)

    for smali_dir in smali_dirs:
        for smali_file in smali_dir.rglob("*.smali"):
            try:
                content = smali_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if 'msauth://' not in content or 'getApplicationId' not in content:
                continue
            new_content = builder_pattern.sub(builder_replacement, content)
            if new_content != content:
                smali_file.write_text(new_content, encoding="utf-8")
                builder_patched = True
                print(f"  ✓ 硬编码 redirect URI builder: {smali_file.relative_to(work_dir)}")
                print(f"    URI = {redirect_uri}")

    if not builder_patched:
        print("  [WARN] 未找到 redirect URI builder 代码")

    # === Part 2: 绕过 OneAuth redirect URI 校验 ===
    # OneAuth.smali 中比较计算出的 URI 与配置的 URI，不匹配则报错闪退
    # 将 if-nez (匹配才跳过) 改为 goto (始终跳过错误)
    oneauth_patched = False
    oneauth_pattern = re.compile(
        r'(    invoke-virtual \{v4, p1\}, '
        r'Ljava/lang/String;->equals\(Ljava/lang/Object;\)Z\n'
        r'\n'
        r'    move-result v5\n'
        r'\n'
        r')    if-nez v5, :cond_4'
    )

    for smali_dir in smali_dirs:
        oneauth_file = smali_dir / "com" / "microsoft" / "authentication" / "OneAuth.smali"
        if not oneauth_file.exists():
            continue
        content = oneauth_file.read_text(encoding="utf-8")
        if 'redirect_uri mismatch' not in content:
            continue
        new_content = oneauth_pattern.sub(r'\1    goto :cond_4', content)
        if new_content != content:
            oneauth_file.write_text(new_content, encoding="utf-8")
            oneauth_patched = True
            print("  ✓ 绕过 OneAuth redirect URI 校验")

    if not oneauth_patched:
        print("  [WARN] 未找到 OneAuth 校验代码")

    return (1 if builder_patched else 0) + (1 if oneauth_patched else 0)


def patch_auto_skip_dialogs(work_dir):
    """
    自动跳过/调整初次登录后的界面 (可选):
    1. UnifiedConsentDialog ("让我们一起做的更好") - 跳过隐私同意弹窗
    2. Fre4vActivity 联系人同步开关 - 默认取消勾选
    3. OptionalTelemetryDialogFragment - 自动点击 "不发送"
    4. ContactSyncDialogFragment - 自动点击 "以后再说"
    """
    print("\n[*] Patch: 自动跳过/调整初始化界面")

    work = Path(work_dir)
    patch_count = 0

    # === 1. UnifiedConsentDialog ===
    consent_mgr = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/utilities/UnifiedConsentManager"
    )
    if consent_mgr:
        content = consent_mgr.read_text(encoding="utf-8")
        pattern = (
            r'(\.method public final checkConsentAndDisplayDialog'
            r'\(Lcom/microsoft/teams/mobile/views/activities/MainActivity;\)V)'
            r'.*?'
            r'(\.end method)'
        )
        replacement = (
            r'\1\n'
            '    .locals 0\n'
            '\n'
            '    # [CHINA-PATCH] 跳过隐私同意弹窗\n'
            '    return-void\n'
            r'\2'
        )
        new_content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
        if new_content != content:
            consent_mgr.write_text(new_content, encoding="utf-8")
            patch_count += 1
            print("  ✓ UnifiedConsentManager: 跳过隐私同意弹窗")
        else:
            print("  [INFO] UnifiedConsentManager: 方法签名不匹配，跳过")
    else:
        print("  [INFO] UnifiedConsentManager.smali 不存在，跳过")

    # === 2. Fre4vActivity: 默认取消勾选联系人同步 ===
    fre_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/views/activities/Fre4vActivity"
    )
    if fre_file:
        content = fre_file.read_text(encoding="utf-8")
        # 查找 mSyncContactsChecked 初始化: const/4 vN, 0x1 后跟 iput-boolean
        pattern = re.compile(
            r'(    const/4 (v\d+), )0x1(\n'
            r'\n'
            r'    iput-boolean \2, p0, '
            r'Lcom/microsoft/skype/teams/views/activities/Fre4vActivity;'
            r'->mSyncContactsChecked:Z)'
        )
        new_content = pattern.sub(r'\g<1>0x0\3', content)
        if new_content != content:
            fre_file.write_text(new_content, encoding="utf-8")
            patch_count += 1
            print("  ✓ Fre4vActivity: 默认取消勾选联系人同步")
        else:
            print("  [INFO] Fre4vActivity: 未找到 mSyncContactsChecked 初始化，跳过")
    else:
        print("  [INFO] Fre4vActivity.smali 不存在，跳过")

    # === 3. OptionalTelemetryDialogFragment ===
    telemetry_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/views/fragments/Dialogs/OptionalTelemetryDialogFragment"
    )
    if telemetry_file:
        content = telemetry_file.read_text(encoding="utf-8")
        old_tail = (
            'invoke-virtual {p1, p2}, Landroid/view/View;'
            '->setOnClickListener(Landroid/view/View$OnClickListener;)V\n'
            '\n'
            '    return-void\n'
            '.end method'
        )
        new_tail = (
            'invoke-virtual {p1, p2}, Landroid/view/View;'
            '->setOnClickListener(Landroid/view/View$OnClickListener;)V\n'
            '\n'
            '    # [CHINA-PATCH] 自动点击 "不发送" 按钮\n'
            '    iget-object v0, p0, Lcom/microsoft/skype/teams/views/fragments/'
            'Dialogs/OptionalTelemetryDialogFragment;->mDeclineButton:'
            'Lcom/microsoft/stardust/ButtonView;\n'
            '\n'
            '    invoke-virtual {v0}, Landroid/view/View;->performClick()Z\n'
            '\n'
            '    return-void\n'
            '.end method'
        )
        if old_tail in content:
            content = content.replace(old_tail, new_tail, 1)
            telemetry_file.write_text(content, encoding="utf-8")
            patch_count += 1
            print("  ✓ OptionalTelemetryDialogFragment: 自动点击 '不发送'")
        else:
            print("  [INFO] OptionalTelemetryDialogFragment: 模式不匹配，跳过")
    else:
        print("  [INFO] OptionalTelemetryDialogFragment.smali 不存在，跳过")

    # === 4. ContactSyncDialogFragment ===
    contact_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/views/fragments/Dialogs/ContactSyncDialogFragment"
    )
    if contact_file:
        content = contact_file.read_text(encoding="utf-8")
        old_tail = (
            'invoke-virtual {p2, p1, v2, v0, v1}, '
            'Lcom/microsoft/teams/remoteasset/RemoteAssetManager;->show'
            '(Lcom/microsoft/teams/remoteasset/models/RemoteImage$Image;'
            'ILcom/microsoft/stardust/ImageView;'
            'Lcom/microsoft/teams/core/services/IScenarioManager;)V\n'
            '\n'
            '    return-void\n'
            '.end method'
        )
        new_tail = (
            'invoke-virtual {p2, p1, v2, v0, v1}, '
            'Lcom/microsoft/teams/remoteasset/RemoteAssetManager;->show'
            '(Lcom/microsoft/teams/remoteasset/models/RemoteImage$Image;'
            'ILcom/microsoft/stardust/ImageView;'
            'Lcom/microsoft/teams/core/services/IScenarioManager;)V\n'
            '\n'
            '    # [CHINA-PATCH] 自动点击 "以后再说" 按钮\n'
            '    iget-object v0, p0, Lcom/microsoft/skype/teams/views/fragments/'
            'Dialogs/ContactSyncDialogFragment;->mLaterButton:'
            'Landroid/widget/TextView;\n'
            '\n'
            '    invoke-virtual {v0}, Landroid/view/View;->performClick()Z\n'
            '\n'
            '    return-void\n'
            '.end method'
        )
        if old_tail in content:
            content = content.replace(old_tail, new_tail, 1)
            contact_file.write_text(content, encoding="utf-8")
            patch_count += 1
            print("  ✓ ContactSyncDialogFragment: 自动点击 '以后再说'")
        else:
            print("  [INFO] ContactSyncDialogFragment: 模式不匹配，跳过")
    else:
        print("  [INFO] ContactSyncDialogFragment.smali 不存在，跳过")

    print(f"\n  自动跳过弹窗: 成功 {patch_count} 处")
    return patch_count


def patch_fix_incoming_calls(work_dir):
    """
    修复个人账户来电接收问题。

    根因: CallManager 构造函数中 Premature Notification Flow 被禁用:
        mPrematureNotificationFlowEnabled = getEcsSettingAsBoolean(
            "PREMATURE_NOTIFICATION_FLOW_ENABLED", isDevDebug()
        ) && !isChinaPushTransport();

    双重限制:
      1. ECS 设置在中国版服务端不存在，default = isDevDebug() = false
      2. isChinaPushTransport() = isBaidu() = true，进一步强制禁用

    Premature Notification Flow 用于在 SkyLib 引擎初始化前提前显示来电通知。
    禁用后，服务端等待客户端响应超时 → 报告"用户不在线"。

    修复: 找到 iput-boolean vN, ..., mPrematureNotificationFlowEnabled:Z，
    在其前插入 const/4 vN, 0x1 强制启用。
    """
    print("\n[*] Patch: 修复来电接收 (启用 Premature Notification Flow)")

    call_manager_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/calling/call/CallManager"
    )

    if not call_manager_file:
        print("  [ERROR] 未找到 CallManager.smali")
        return 0

    content = call_manager_file.read_text(encoding="utf-8")

    # 找到 mPrematureNotificationFlowEnabled 的 iput-boolean 赋值
    # smali 模式:
    #   iput-boolean vN, vM, Lcom/.../CallManager;->mPrematureNotificationFlowEnabled:Z
    iput_pattern = re.compile(
        r'(    iput-boolean (v\d+), v\d+, '
        r'Lcom/microsoft/skype/teams/calling/call/CallManager;'
        r'->mPrematureNotificationFlowEnabled:Z)'
    )

    match = iput_pattern.search(content)
    if not match:
        print("  [ERROR] 未找到 mPrematureNotificationFlowEnabled 赋值位置")
        return 0

    reg = match.group(2)  # 寄存器名 (如 v0)
    full_line = match.group(1)

    # 在 iput-boolean 前插入 const/4 vN, 0x1 强制启用
    inject = (
        f'    # [CHINA-PATCH] 强制启用 Premature Notification Flow (来电接收修复)\n'
        f'    const/4 {reg}, 0x1\n'
        f'\n'
        f'{full_line}'
    )
    new_content = content.replace(full_line, inject, 1)
    if new_content == content:
        print("  [ERROR] 替换未生效")
        return 0

    call_manager_file.write_text(new_content, encoding="utf-8")

    print(f"  ✓ CallManager.<init>: mPrematureNotificationFlowEnabled = true")
    print(f"    寄存器: {reg}")
    print(f"    效果: 来电通知将在 SkyLib 初始化前立即显示")
    print(f"    文件: {call_manager_file.relative_to(work_dir)}")
    return 1


def rebuild_apk(work_dir, output_apk):
    """使用 apktool 重新构建 APK"""
    print(f"\n[3/5] 重新构建 APK")
    run_cmd([
        "apktool", "b",
        "-o", str(output_apk),
        work_dir,
    ], java_heap=True)
    print(f"  ✓ 构建完成 → {output_apk}")


def generate_keystore(keystore_path):
    """生成签名密钥库（环境变量提供了外部密钥库时跳过生成）"""
    if os.path.exists(keystore_path):
        print(f"  密钥库已存在: {keystore_path}")
        return
    if os.environ.get("KEYSTORE_FILE"):
        raise FileNotFoundError(f"环境变量指定的密钥库不存在: {keystore_path}")
    print(f"  生成签名密钥库: {keystore_path}")
    run_cmd([
        "keytool", "-genkeypair", "-v",
        "-keystore", keystore_path,
        "-alias", KEY_ALIAS,
        "-keyalg", "RSA", "-keysize", "2048",
        "-validity", "10000",
        "-storepass", KEYSTORE_PASS,
        "-keypass", KEY_PASS,
        "-dname", "CN=ChinaPatch, OU=Dev, O=Dev, L=City, ST=State, C=US",
    ])


def sign_and_align(input_apk, output_apk, has_apksigner):
    """对齐并签名 APK"""
    print(f"\n[4/5] 对齐并签名 APK")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    keystore_path = os.path.join(script_dir, KEYSTORE_FILE)
    generate_keystore(keystore_path)

    aligned_apk = str(input_apk) + ".aligned"
    run_cmd(["zipalign", "-f", "-p", "4", str(input_apk), aligned_apk])
    print("  ✓ zipalign 完成")

    if has_apksigner:
        run_cmd([
            "apksigner", "sign",
            "--min-sdk-version", "26",
            "--ks", keystore_path,
            "--ks-pass", f"pass:{KEYSTORE_PASS}",
            "--ks-key-alias", KEY_ALIAS,
            "--key-pass", f"pass:{KEY_PASS}",
            "--out", str(output_apk),
            aligned_apk,
        ])
    else:
        shutil.copy2(aligned_apk, str(output_apk))
        run_cmd([
            "jarsigner", "-verbose",
            "-sigalg", "SHA256withRSA", "-digestalg", "SHA-256",
            "-keystore", keystore_path,
            "-storepass", KEYSTORE_PASS,
            "-keypass", KEY_PASS,
            str(output_apk), KEY_ALIAS,
        ])

    if os.path.exists(aligned_apk):
        os.remove(aligned_apk)
    print(f"  ✓ 签名完成 → {output_apk}")


def strip_architectures(work_dir, keep_arch):
    """
    移除不需要的原生库架构，仅保留指定架构。
    keep_arch: 要保留的架构，如 "arm64-v8a"
    """
    lib_dir = Path(work_dir) / "lib"
    if not lib_dir.exists():
        print(f"  [WARN] 未找到 lib/ 目录")
        return

    all_archs = sorted(d.name for d in lib_dir.iterdir() if d.is_dir())
    keep_dir = lib_dir / keep_arch

    if not keep_dir.exists():
        print(f"  [ERROR] 指定架构 {keep_arch} 不存在! 可用: {', '.join(all_archs)}")
        sys.exit(1)

    removed_archs = []
    saved_bytes = 0
    for arch_dir in lib_dir.iterdir():
        if arch_dir.is_dir() and arch_dir.name != keep_arch:
            dir_size = sum(f.stat().st_size for f in arch_dir.rglob("*") if f.is_file())
            saved_bytes += dir_size
            shutil.rmtree(arch_dir)
            removed_archs.append(arch_dir.name)

    if removed_archs:
        print(f"  保留架构: {keep_arch}")
        print(f"  移除架构: {', '.join(removed_archs)}")
        print(f"  预计减少: {saved_bytes / 1024 / 1024:.1f} MB")
    else:
        print(f"  仅有 {keep_arch} 架构，无需裁剪")


def verify_apk(apk_path):
    """验证 APK 基本结构"""
    print(f"\n[5/5] 验证 APK")
    try:
        with zipfile.ZipFile(str(apk_path), 'r') as z:
            names = z.namelist()
            has_manifest = "AndroidManifest.xml" in names
            has_dex = any(n.endswith(".dex") for n in names)
            has_resources = "resources.arsc" in names
            print(f"  AndroidManifest.xml: {'✓' if has_manifest else '✗'}")
            print(f"  classes.dex:         {'✓' if has_dex else '✗'}")
            print(f"  resources.arsc:      {'✓' if has_resources else '✗'}")

            if not (has_manifest and has_dex and has_resources):
                print("  [ERROR] APK 结构不完整!")
                return False
    except zipfile.BadZipFile:
        print("  [ERROR] 无效的 ZIP/APK 文件!")
        return False

    if shutil.which("apksigner"):
        result = run_cmd(
            ["apksigner", "verify", "--verbose", str(apk_path)],
            check=False, capture=True,
        )
        if result.returncode == 0:
            print("  签名验证: ✓")
        else:
            print("  签名验证: ✗")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Microsoft Teams 国内版个人账户启用工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
原理:
    国内版 Teams (百度渠道) 通过 isBaidu()=true → enableConsumerTenant=false
    禁用了个人 (MSA/Consumer) 账户登录。本工具将 enableConsumerTenant 强制
    设为 true，启用完整的个人账户登录功能。

    MSA 认证基础设施 (Client ID、OneAuth 配置) 在 APK 中完整存在，
    仅需解除客户端限制即可。

示例:
    python3 patch_china_teams.py teams-china.apk
    python3 patch_china_teams.py teams-china.apk --output teams-patched.apk
    python3 patch_china_teams.py teams-china.apk --skip-dialogs
    python3 patch_china_teams.py teams-china.apk --fix-incoming-calls
    python3 patch_china_teams.py teams-china.apk --arch arm64-v8a
        """,
    )
    parser.add_argument("input_apk", help="输入的国内版 Teams APK 文件路径")
    parser.add_argument("--output", "-o", help="输出路径 (默认: <input>-patched.apk)")
    parser.add_argument("--skip-dialogs", action="store_true",
                        help="同时跳过隐私同意/诊断数据/联系人同步弹窗")
    parser.add_argument("--arch", default=None,
                        help="仅保留指定架构的原生库 (如 arm64-v8a)，移除其他架构以减小 APK 体积")
    parser.add_argument("--fix-incoming-calls", action="store_true",
                        help="修复个人账户来电接收 (启用 Premature Notification Flow)")
    parser.add_argument("--keep-work-dir", action="store_true",
                        help="保留反编译工作目录")

    args = parser.parse_args()

    input_apk = Path(args.input_apk).resolve()
    if not input_apk.exists():
        print(f"[ERROR] 文件不存在: {input_apk}")
        sys.exit(1)

    if args.output:
        output_apk = Path(args.output).resolve()
    else:
        suffix = "-patched"
        if args.arch:
            suffix += f"-{args.arch}"
        output_apk = input_apk.with_name(input_apk.stem + suffix + input_apk.suffix)

    print("=" * 60)
    print("Microsoft Teams 国内版 — 个人账户启用工具")
    print("=" * 60)
    print(f"输入: {input_apk}")
    print(f"输出: {output_apk}")
    print(f"跳过弹窗: {'是' if args.skip_dialogs else '否'}")
    print(f"修复来电: {'是' if args.fix_incoming_calls else '否'}")
    print(f"架构裁剪: {args.arch if args.arch else '否 (保留全部)'}")

    has_apksigner = check_dependencies()

    work_dir = str(input_apk.parent / "china-apk-work")
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)

    try:
        # Step 1: 反编译
        decompile_apk(input_apk, work_dir)

        # Step 2: Patch enableConsumerTenant (核心 patch)
        patch_enable_consumer_tenant(work_dir)

        # Step 2b: Patch redirect URI (签名变更后必须)
        patch_redirect_uri(work_dir)

        # Step 2c: 可选 — 跳过弹窗
        if args.skip_dialogs:
            patch_auto_skip_dialogs(work_dir)

        # Step 2d: 可选 — 修复来电接收
        if args.fix_incoming_calls:
            patch_fix_incoming_calls(work_dir)

        # Step 2e: 可选 — 裁剪架构
        if args.arch:
            print(f"\n[*] 裁剪原生库架构: 仅保留 {args.arch}")
            strip_architectures(work_dir, args.arch)

        # Step 3: 重新构建
        unsigned_apk = output_apk.with_name(output_apk.stem + "-unsigned.apk")
        rebuild_apk(work_dir, unsigned_apk)

        # Step 4: 签名
        sign_and_align(unsigned_apk, output_apk, has_apksigner)

        # 清理 unsigned
        if unsigned_apk.exists():
            unsigned_apk.unlink()

        # Step 5: 验证
        verify_apk(output_apk)

        print("\n" + "=" * 60)
        print("处理完成!")
        print(f"输出文件: {output_apk}")
        print(f"文件大小: {output_apk.stat().st_size / 1024 / 1024:.1f} MB")
        print("=" * 60)
        print("\n功能说明:")
        print("  ✓ 个人 (MSA) 账户登录已启用")
        print("  ✓ 企业 (AAD) 账户登录不受影响")
        print("  ✓ 国内推送通知渠道保持正常 (百度/小米/华为等)")
        print("  ✓ 隐私声明功能保持正常")
        print("\n注意事项:")
        print("  1. 签名已更改，无法直接覆盖安装原版，需卸载后安装")
        print("  2. 个人账户使用全球 Microsoft 登录服务 (login.microsoftonline.com)")
        print("  3. 如遇服务端限制，个人账户可能无法登录")

    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] 命令执行失败: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if not args.keep_work_dir and os.path.exists(work_dir):
            print(f"\n清理工作目录: {work_dir}")
            shutil.rmtree(work_dir)


if __name__ == "__main__":
    main()
