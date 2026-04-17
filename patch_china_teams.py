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
    4. AuthorizationService.addConsumerTenantIfNecessary() → 忽略 consumerMTBlocked
    5. AuthorizationService.createConsumerTenant() → 固定使用 "个人" 文案

    兼容新版变化:
    - 新版 AppConfigurationImpl.enableConsumerTenant() 额外受 ECS 开关
      "disableConsumerSignIn" 控制；直接替换整个方法体可一并绕过。
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

    # === Patch 4: AuthorizationService.addConsumerTenantIfNecessary() ===
    #
    # 新版若服务端返回 AuthenticatedUser.consumerMTBlocked=true，
    # 即使 enableConsumerTenant 已打开，也不会把 Consumer tenant
    # 加入租户列表，最终表现为“没有组织/没有租户可选”。
    #
    # 目标: 忽略 consumerMTBlocked，缺少 consumer tenant 时始终补入。

    authz_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/authorization/AuthorizationService"
    )

    if authz_file:
        content = authz_file.read_text(encoding="utf-8")
        blocked_pattern = re.compile(
            r'(    iget-boolean p3, p2, '
            r'Lcom/microsoft/skype/teams/models/AuthenticatedUser;'
            r'->consumerMTBlocked:Z)'
        )
        match = blocked_pattern.search(content)
        if match:
            full_line = match.group(1)
            inject = (
                '    # [CHINA-PATCH] 忽略 consumerMTBlocked，强制补入 consumer tenant\n'
                '    const/4 p3, 0x0'
            )
            new_content = content.replace(full_line, inject, 1)
            if new_content != content:
                authz_file.write_text(new_content, encoding="utf-8")
                patch_count += 1
                print("  ✓ AuthorizationService.addConsumerTenantIfNecessary() → 忽略 consumerMTBlocked")
                print(f"    文件: {authz_file.relative_to(work_dir)}")
        else:
            print("  [WARN] AuthorizationService: 未找到 consumerMTBlocked 检查")

        rename_pattern = re.compile(
            r'(    if-eqz p2, :cond_0)'
        )
        rename_match = rename_pattern.search(content)
        if rename_match:
            old_line = rename_match.group(1)
            new_line = (
                '    # [CHINA-PATCH] 固定使用 consumer_tenant_name，避免显示个人昵称为组织名\n'
                '    goto :cond_0'
            )
            new_content = content.replace(old_line, new_line, 1)
            if new_content != content:
                authz_file.write_text(new_content, encoding="utf-8")
                content = new_content
                patch_count += 1
                print("  ✓ AuthorizationService.createConsumerTenant() → 固定使用默认 consumer 名称")
                print(f"    文件: {authz_file.relative_to(work_dir)}")
        else:
            print("  [WARN] AuthorizationService: 未找到 createConsumerTenant 重命名分支")
    else:
        print("  [WARN] 未找到 AuthorizationService.smali")

    if patch_count == 0:
        print("  [ERROR] 未能成功应用任何 Consumer Tenant patch!")
        sys.exit(1)
    else:
        print(f"\n  总计成功应用 {patch_count} 处 Consumer Tenant patch")

    return patch_count


def patch_tfl_post_login_chain(work_dir):
    """
    修复新版登录后的 TFL 请求链路。

    目标:
    1. TflRequestInterceptor 遇到 token/auth 异常时 fail-open，避免直接把用户踢回登录页
    2. IntegrityChallengeInterceptor 不再触发新版完整性挑战逻辑
    3. TeamsLicenseRepository 不再主动发起 license 刷新，并始终视作已有 Teams license
    4. TeamsNavigationService 不再因 SSO emails 为空强制跳回 FreAuth
    5. FreAuthActivity 忽略 signOut/resetUser 分支，避免再次回首页
    """
    print("\n[*] Patch: 修复 TFL 登录后链路")
    patch_count = 0

    # === Patch 1: TflRequestInterceptor.throwAuthError() ===
    tfl_interceptor_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/data/proxy/TflRequestInterceptor"
    )

    if not tfl_interceptor_file:
        print("  [ERROR] 未找到 TflRequestInterceptor.smali")
    else:
        content = tfl_interceptor_file.read_text(encoding="utf-8")
        pattern = (
            r'(\.method public static throwAuthError'
            r'\(Lcom/microsoft/skype/teams/data/BaseException;'
            r'Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;\)V)'
            r'.*?'
            r'(\.end method)'
        )
        replacement = (
            r'\1\n'
            '    .locals 0\n'
            '\n'
            '    # [CHINA-PATCH] TFL 后续接口失败时 fail-open，避免直接回到登录首页\n'
            '    return-void\n'
            r'\2'
        )
        new_content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
        if new_content != content:
            tfl_interceptor_file.write_text(new_content, encoding="utf-8")
            patch_count += 1
            print("  ✓ TflRequestInterceptor.throwAuthError() → fail-open")
            print(f"    文件: {tfl_interceptor_file.relative_to(work_dir)}")
        else:
            print("  [WARN] TflRequestInterceptor: 未找到 throwAuthError 方法")

    # === Patch 2: IntegrityChallengeInterceptor.intercept() ===
    integrity_file = find_smali_file(
        work_dir,
        "com/microsoft/teams/appintegrity/IntegrityChallengeInterceptor"
    )

    if not integrity_file:
        print("  [ERROR] 未找到 IntegrityChallengeInterceptor.smali")
    else:
        content = integrity_file.read_text(encoding="utf-8")
        pattern = (
            r'(\.method public final intercept'
            r'\(Lokhttp3/Interceptor\$Chain;\)Lokhttp3/Response;)'
            r'.*?'
            r'(\.end method)'
        )
        replacement = (
            r'\1\n'
            '    .locals 1\n'
            '\n'
            '    const-string v0, "chain"\n'
            '\n'
            '    invoke-static {p1, v0}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V\n'
            '\n'
            '    invoke-interface {p1}, Lokhttp3/Interceptor$Chain;->request()Lokhttp3/Request;\n'
            '\n'
            '    move-result-object v0\n'
            '\n'
            '    invoke-interface {p1, v0}, Lokhttp3/Interceptor$Chain;->proceed(Lokhttp3/Request;)Lokhttp3/Response;\n'
            '\n'
            '    move-result-object v0\n'
            '\n'
            '    return-object v0\n'
            r'\2'
        )
        new_content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
        if new_content != content:
            integrity_file.write_text(new_content, encoding="utf-8")
            patch_count += 1
            print("  ✓ IntegrityChallengeInterceptor.intercept() → 跳过完整性挑战")
            print(f"    文件: {integrity_file.relative_to(work_dir)}")
        else:
            print("  [WARN] IntegrityChallengeInterceptor: 未找到 intercept 方法")

    # === Patch 3/4/5: TeamsLicenseRepository ===
    license_repo_file = find_smali_file(
        work_dir,
        "com/microsoft/teams/license/TeamsLicenseRepository"
    )

    if not license_repo_file:
        print("  [ERROR] 未找到 TeamsLicenseRepository.smali")
    else:
        content = license_repo_file.read_text(encoding="utf-8")

        patterns = [
            (
                r'(\.method public final getProbablyHasTeamsLicense\(\)Z)'
                r'.*?'
                r'(\.end method)',
                r'\1\n'
                '    .locals 1\n'
                '\n'
                '    const/4 v0, 0x1\n'
                '\n'
                '    return v0\n'
                r'\2',
                "getProbablyHasTeamsLicense"
            ),
            (
                r'(\.method public final requestRefreshLicenseDetails\(Z\)V)'
                r'.*?'
                r'(\.end method)',
                r'\1\n'
                '    .locals 0\n'
                '\n'
                '    return-void\n'
                r'\2',
                "requestRefreshLicenseDetails(Z)"
            ),
            (
                r'(\.method public final requestRefreshLicenseDetails'
                r'\(JLkotlin/coroutines/jvm/internal/ContinuationImpl;\)'
                r'Ljava/lang/Object;)'
                r'.*?'
                r'(\.end method)',
                r'\1\n'
                '    .locals 1\n'
                '\n'
                '    sget-object v0, Lkotlin/Unit;->INSTANCE:Lkotlin/Unit;\n'
                '\n'
                '    return-object v0\n'
                r'\2',
                "requestRefreshLicenseDetails(J, Continuation)"
            ),
        ]

        for pattern, replacement, label in patterns:
            new_content = re.sub(pattern, replacement, content, count=1, flags=re.DOTALL)
            if new_content != content:
                content = new_content
                patch_count += 1
                print(f"  ✓ TeamsLicenseRepository.{label} 已 patch")
            else:
                print(f"  [WARN] TeamsLicenseRepository: 未找到 {label}")

        license_repo_file.write_text(content, encoding="utf-8")
        print(f"    文件: {license_repo_file.relative_to(work_dir)}")

        print(f"\n  TFL 登录后链路: 成功 {patch_count} 处")
    # === Patch 6: TeamsNavigationService 导航回退链 ===
    nav_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/navigation/TeamsNavigationService"
    )
    nav_lambda_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/navigation/TeamsNavigationService$$ExternalSyntheticLambda35"
    )

    if nav_file:
        nav_content = nav_file.read_text(encoding="utf-8")
        nav_method_pattern = (
            r'(\.method public final navigateToFreAuth'
            r'\(Landroid/content/Context;Lcom/microsoft/skype/teams/models/pojos/FreParameters;ZI\)V)'
            r'.*?'
            r'(\.end method)'
        )
        nav_method_replacement = (
            r'\1\n'
            '    .locals 1\n'
            '\n'
            '    invoke-static {p2}, Lcom/microsoft/teams/utils/NavigationMappersKt;->toFreAuthParamsGeneratorBuilder(Lcom/microsoft/skype/teams/models/pojos/FreParameters;)Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator$Builder;\n'
            '\n'
            '    move-result-object p2\n'
            '\n'
            '    # [CHINA-PATCH] 跳回 FreAuth 时不再带 signOut 语义\n'
            '    const/4 v0, 0x0\n'
            '\n'
            '    iput-boolean v0, p2, Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator$Builder;->signOut:Z\n'
            '\n'
            '    if-eqz p3, :cond_0\n'
            '\n'
            '    const/4 p3, 0x1\n'
            '\n'
            '    iput-boolean p3, p2, Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator$Builder;->bringToFront:Z\n'
            '\n'
            '    new-instance p3, Lcom/microsoft/skype/teams/keys/IntentKey$FreAuthActivityIntentKey;\n'
            '\n'
            '    invoke-virtual {p2}, Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator$Builder;->build()Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator;\n'
            '\n'
            '    move-result-object p2\n'
            '\n'
            '    invoke-direct {p3, p2}, Lcom/microsoft/skype/teams/keys/IntentKey$FreAuthActivityIntentKey;-><init>(Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator;)V\n'
            '\n'
            '    invoke-virtual {p0, p1, p3}, Lcom/microsoft/skype/teams/services/navigation/TeamsNavigationService;->navigateWithIntentKey(Landroid/content/Context;Lcom/microsoft/skype/teams/keys/BaseIntentKey;)Lbolts/Task;\n'
            '\n'
            '    goto :goto_0\n'
            '\n'
            '    :cond_0\n'
            '    iput p4, p2, Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator$Builder;->flags:I\n'
            '\n'
            '    new-instance p3, Lcom/microsoft/skype/teams/keys/IntentKey$FreAuthActivityIntentKey;\n'
            '\n'
            '    invoke-virtual {p2}, Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator$Builder;->build()Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator;\n'
            '\n'
            '    move-result-object p2\n'
            '\n'
            '    invoke-direct {p3, p2}, Lcom/microsoft/skype/teams/keys/IntentKey$FreAuthActivityIntentKey;-><init>(Lcom/microsoft/skype/teams/activity/FreAuthParamsGenerator;)V\n'
            '\n'
            '    invoke-virtual {p0, p1, p3}, Lcom/microsoft/skype/teams/services/navigation/TeamsNavigationService;->navigateWithIntentKey(Landroid/content/Context;Lcom/microsoft/skype/teams/keys/BaseIntentKey;)Lbolts/Task;\n'
            '\n'
            '    :goto_0\n'
            '    return-void\n'
            r'\2'
        )
        new_nav_content = re.sub(nav_method_pattern, nav_method_replacement, nav_content, count=1, flags=re.DOTALL)
        if new_nav_content != nav_content:
            nav_file.write_text(new_nav_content, encoding="utf-8")
            patch_count += 1
            print("  ✓ TeamsNavigationService.navigateToFreAuth() → 清除 signOut 参数")
            print(f"    文件: {nav_file.relative_to(work_dir)}")
        else:
            print("  [WARN] TeamsNavigationService: 未找到 navigateToFreAuth 方法")
    else:
        print("  [WARN] 未找到 TeamsNavigationService.smali")

    if nav_lambda_file:
        lambda_content = nav_lambda_file.read_text(encoding="utf-8")
        lambda_patterns = [
            (
                '    invoke-virtual {v0, v6, v7, v8, v9}, Lcom/microsoft/skype/teams/services/navigation/TeamsNavigationService;->navigateToFreAuth(Landroid/content/Context;Lcom/microsoft/skype/teams/models/pojos/FreParameters;ZI)V\n'
                '\n'
                '    goto :goto_2',
                '    # [CHINA-PATCH] SSO emails 为空时保持当前流程，不再强制跳回 FreAuth\n'
                '    goto :goto_2'
            ),
            (
                '    invoke-virtual {v0, v6, v7, v8, v9}, Lcom/microsoft/skype/teams/services/navigation/TeamsNavigationService;->navigateToFreAuth(Landroid/content/Context;Lcom/microsoft/skype/teams/models/pojos/FreParameters;ZI)V\n'
                '\n'
                '    :goto_2',
                '    # [CHINA-PATCH] get SSO accounts task 未完成时不再强制回 FreAuth\n'
                '    :goto_2'
            ),
            (
                '    invoke-interface {v2, v4, v5, p1, v1}, Lcom/microsoft/teams/nativecore/logger/ILogger;->log(ILjava/lang/String;Ljava/lang/String;[Ljava/lang/Object;)V\n'
                '\n'
                '    invoke-virtual {v0, v6, v7, v8, v9}, Lcom/microsoft/skype/teams/services/navigation/TeamsNavigationService;->navigateToFreAuth(Landroid/content/Context;Lcom/microsoft/skype/teams/models/pojos/FreParameters;ZI)V\n'
                '\n'
                '    goto :goto_2',
                '    invoke-interface {v2, v4, v5, p1, v1}, Lcom/microsoft/teams/nativecore/logger/ILogger;->log(ILjava/lang/String;Ljava/lang/String;[Ljava/lang/Object;)V\n'
                '\n'
                '    # [CHINA-PATCH] SSO emails 为空时保持当前流程，不再强制跳回 FreAuth\n'
                '    goto :goto_2'
            ),
        ]
        updated = False
        for old, new in lambda_patterns:
            if old in lambda_content:
                lambda_content = lambda_content.replace(old, new, 1)
                updated = True
        if updated:
            nav_lambda_file.write_text(lambda_content, encoding="utf-8")
            patch_count += 1
            print("  ✓ TeamsNavigationService$$ExternalSyntheticLambda35 → 禁止 SSO fallback 跳回 FreAuth")
            print(f"    文件: {nav_lambda_file.relative_to(work_dir)}")
        else:
            print("  [WARN] TeamsNavigationService$$ExternalSyntheticLambda35: 未找到 SSO fallback 跳转点")
    else:
        print("  [WARN] 未找到 TeamsNavigationService$$ExternalSyntheticLambda35.smali")

    # === Patch 7: FreAuthActivity 忽略 signOut resetUser 分支 ===
    freauth_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/views/activities/FreAuthActivity"
    )

    if freauth_file:
        freauth_content = freauth_file.read_text(encoding="utf-8")
        signout_pattern = re.compile(
            r'(    iget-boolean v2, v5, '
            r'Lcom/microsoft/skype/teams/models/pojos/FreParameters;->signOut:Z\n)'
            r'(\n\s*if-eqz v2, :cond_3c)'
        )
        new_freauth = signout_pattern.sub(
            r'\1'
            '    # [CHINA-PATCH] 忽略 signOut/resetUser 分支，避免再次回首页\n'
            '    const/4 v2, 0x0\n'
            r'\2',
            freauth_content,
            count=1,
        )
        if new_freauth != freauth_content:
            freauth_content = new_freauth
            freauth_file.write_text(new_freauth, encoding="utf-8")
            patch_count += 1
            print("  ✓ FreAuthActivity → 忽略 signOut resetUser 分支")
            print(f"    文件: {freauth_file.relative_to(work_dir)}")
        else:
            print("  [WARN] FreAuthActivity: 未找到 signOut resetUser 分支")

        reset_calls_pattern = re.compile(
            r'(^\s*)invoke-interface \{[vp]\d+\}, '
            r'Lcom/microsoft/skype/teams/services/authorization/IAuthorizationService;'
            r'->resetUser\(\)V',
            flags=re.MULTILINE,
        )
        new_freauth = reset_calls_pattern.sub(
            r'\1# [CHINA-PATCH] 禁用 FreAuthActivity 内部 resetUser，避免跳回首页\n\1nop',
            freauth_content,
        )
        if new_freauth != freauth_content:
            freauth_file.write_text(new_freauth, encoding="utf-8")
            patch_count += 1
            print("  ✓ FreAuthActivity → 禁用剩余 resetUser 调用")
            print(f"    文件: {freauth_file.relative_to(work_dir)}")
    else:
        print("  [WARN] 未找到 FreAuthActivity.smali")

    print(f"\n  TFL 登录后链路: 成功 {patch_count} 处")
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

    根因分析（七重阻断）:

    1. enableTrouterRegistration() 返回 false（主要原因）:
       UserConfiguration.enableTrouterRegistration() 依赖 ECS 设置 "trouterEnabled"
       (默认 false)。当返回 false 时:
       - SkyLibManager.registerTrouter() 不注册 TeamsTrouterListener（来电 Trouter 监听器）
       - CallingTrouter 永远不连接，来电信号无法通过 Trouter 送达
       - CallManager 将 SkyLib 切换为 NAL_QUIET_SUSPENDED_OFFLINE_LEVEL（离线模式）
       - 服务端看到设备离线，报告"用户不在线"

    2. TeamsTrouterListener 未写入 TrouterAndroidTeams 的 push routing path:
       官方 TFL 路径会把 TrouterAndroidTeams 归入 PushNotification，再由
       TflRegistrarHelper.getTransportRegistrationArrayForPush() 生成 "TFL" 上下文。
       但 TeamsTrouterListener 自己只保存 routingPath 并直接排队 EDF 注册，没有把
       TrouterAndroidTeams 路径写入 LongPollSyncHelper.mNotificationTypeRoutingPathMap。

    3. TflRegistrarHelper 仅注册 MESSAGING 上下文:
       TflRegistrarHelper.getTransportRegistrationArrayForPoll() 硬编码只返回
       "MESSAGING" 上下文。若 TrouterAndroidTeams 路径没有进入 PushNotification，
       那么 TROUTER transport 最终只剩消息相关上下文。

    4. Notification filter 去重导致 TeamsTrouterListener 注册被跳过:
       LongPollSyncHelper.registerNotificationFilterForRegistrationId() 会对通知过滤设置做
       diff-hash 去重，命中后直接返回 "REGISTRATION_SKIPPED"。实测日志里 TeamsTrouterListener
       会长期卡在这一步，即使前面的 Trouter 连接和 EDF body 构造已经完成。

    5. China push transport 构建分支仍在生效:
       即使前面的 calling 注册已经打通，AppConfigurationImpl.isChinaPushTransport() 仍然
       因 isBaidu() 返回 true。calling/longpoll/notification 相关代码仍会走 China build
       专属分支，和全球版 TFL calling 行为不一致。

    6. Skype endpoint 被 ECS 强制走 message poll 分支:
       LongPollSyncHelper 在 PNHEndpointType.Skype 下会读取 ECS
       "skypeMessagePollEnabled"。当其为 true 时，TFL 的 TROUTER 注册会走
       getTransportRegistrationArrayForPoll()，最终只上报消息上下文；
       当其为 false 时，才会走 getTransportRegistrationArrayForPush()，使用 TFL
       push 语义。当前日志中 Skype endpoint 一直被拉去走 poll 分支。

    7. Premature Notification Flow 被禁用（次要原因）:
       CallManager.<init> 中 mPrematureNotificationFlowEnabled 依赖不存在的 ECS
       设置且被 isChinaPushTransport() 进一步强制禁用。此流程用于在 SkyLib 初始化
       前提前显示来电通知（应用在后台时冷启动场景）。

    修复方案:
    - Patch 1: enableTrouterRegistration() 强制返回 true → 启用 Calling Trouter
    - Patch 2: TeamsTrouterListener 写入/移除 PushNotification routing path
    - Patch 3: TflRegistrarHelper 复用传入的 context map → 至少保留现有 Trouter contexts
    - Patch 4: 关闭 notification filter 的重复设置短路 → 避免 REGISTRATION_SKIPPED
    - Patch 5: isChinaPushTransport() 强制返回 false → 走全球 TFL calling 分支
    - Patch 6: Skype endpoint 强制走 push/TFL 分支，而非 message poll
    - Patch 7: mPrematureNotificationFlowEnabled 强制为 true → 启用推送来电流程
    """
    print("\n[*] Patch: 修复来电接收")
    patch_count = 0

    # === Patch 1: UserConfiguration.enableTrouterRegistration() → true ===
    # 关键修复: 使 SkyLibManager 注册 TeamsTrouterListener，保持 SkyLib 在线
    print("\n  --- Patch 1: 启用 Calling Trouter 注册 ---")

    user_config_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/configuration/UserConfiguration"
    )

    if not user_config_file:
        print("  [ERROR] 未找到 UserConfiguration.smali")
    else:
        uc_content = user_config_file.read_text(encoding="utf-8")

        # 找到 enableTrouterRegistration 方法并在 .registers/.locals 行后插入 return true
        # 注意: JADX 输出 .registers，apktool 输出 .locals
        etr_pattern = re.compile(
            r'(\.method public final enableTrouterRegistration\(\)Z\s*'
            r'\.(?:registers|locals) \d+)\s*\n'
        )

        match = etr_pattern.search(uc_content)
        if not match:
            print("  [ERROR] 未找到 enableTrouterRegistration 方法")
        else:
            inject = (
                f'{match.group(1)}\n'
                f'\n'
                f'    # [CHINA-PATCH] 强制启用 Trouter 注册 (来电接收核心修复)\n'
                f'    const/4 v0, 0x1\n'
                f'\n'
                f'    return v0\n'
                f'\n'
            )
            new_uc = uc_content.replace(match.group(0), inject, 1)
            if new_uc != uc_content:
                user_config_file.write_text(new_uc, encoding="utf-8")
                print(f"  ✓ UserConfiguration.enableTrouterRegistration() → true")
                print(f"    效果: TeamsTrouterListener 将注册，SkyLib 保持在线模式")
                print(f"    文件: {user_config_file.relative_to(work_dir)}")
                patch_count += 1
            else:
                print("  [ERROR] enableTrouterRegistration 替换未生效")

    # === Patch 2: TeamsTrouterListener 写入 PushNotification routing path ===
    # 关键修复: TFL 的 TrouterAndroidTeams 路径需要进入 PushNotification，
    # 这样 TROUTER transport 才会生成官方使用的 TFL push context。
    print("\n  --- Patch 2: 写入 TrouterAndroidTeams push routing path ---")

    teams_trouter_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/calling/notification/TeamsTrouterListener"
    )

    if not teams_trouter_file:
        print("  [ERROR] 未找到 TeamsTrouterListener.smali")
    else:
        tt_content = teams_trouter_file.read_text(encoding="utf-8")

        connected_anchor = (
            '    iput-object p1, p0, '
            'Lcom/microsoft/skype/teams/calling/notification/'
            'TeamsTrouterListener;->mRoutingPath:Ljava/lang/String;\n'
        )
        connected_inject = (
            '    iput-object p1, p0, '
            'Lcom/microsoft/skype/teams/calling/notification/'
            'TeamsTrouterListener;->mRoutingPath:Ljava/lang/String;\n'
            '\n'
            '    # [CHINA-PATCH] 将 TrouterAndroidTeams 路径写入 PushNotification map\n'
            '    if-eqz p1, :cond_china_patch_push_done\n'
            '\n'
            '    iget-object v3, p0, Lcom/microsoft/skype/teams/calling/notification/'
            'TeamsTrouterListener;->mLongPollSyncHelper:'
            'Lcom/microsoft/skype/teams/services/longpoll/LongPollSyncHelper;\n'
            '\n'
            '    iget-object v3, v3, Lcom/microsoft/skype/teams/services/longpoll/'
            'LongPollSyncHelper;->mNotificationTypeRoutingPathMap:'
            'Ljava/util/concurrent/ConcurrentHashMap;\n'
            '\n'
            '    const-string v4, "PushNotification"\n'
            '\n'
            '    invoke-virtual {v3, v4, p1}, Ljava/util/concurrent/ConcurrentHashMap;'
            '->put(Ljava/lang/Object;Ljava/lang/Object;)Ljava/lang/Object;\n'
            '\n'
            '    :cond_china_patch_push_done\n'
        )

        disconnected_anchor = (
            '    iput-wide v2, p0, '
            'Lcom/microsoft/skype/teams/calling/notification/'
            'TeamsTrouterListener;->mEdfRegistrationTime:J\n'
        )
        disconnected_inject = (
            '    iput-wide v2, p0, '
            'Lcom/microsoft/skype/teams/calling/notification/'
            'TeamsTrouterListener;->mEdfRegistrationTime:J\n'
            '\n'
            '    # [CHINA-PATCH] 清理 TrouterAndroidTeams push routing path\n'
            '    iget-object v0, p0, Lcom/microsoft/skype/teams/calling/notification/'
            'TeamsTrouterListener;->mLongPollSyncHelper:'
            'Lcom/microsoft/skype/teams/services/longpoll/LongPollSyncHelper;\n'
            '\n'
            '    iget-object v0, v0, Lcom/microsoft/skype/teams/services/longpoll/'
            'LongPollSyncHelper;->mNotificationTypeRoutingPathMap:'
            'Ljava/util/concurrent/ConcurrentHashMap;\n'
            '\n'
            '    const-string v3, "PushNotification"\n'
            '\n'
            '    invoke-virtual {v0, v3}, Ljava/util/concurrent/ConcurrentHashMap;'
            '->remove(Ljava/lang/Object;)Ljava/lang/Object;\n'
        )

        new_tt = tt_content
        if connected_anchor in new_tt:
            new_tt = new_tt.replace(connected_anchor, connected_inject, 1)
        else:
            print("  [ERROR] TeamsTrouterListener.onTrouterConnected 注入点未找到")

        if disconnected_anchor in new_tt:
            new_tt = new_tt.replace(disconnected_anchor, disconnected_inject, 1)
        else:
            print("  [ERROR] TeamsTrouterListener.onTrouterDisconnected 注入点未找到")

        if new_tt != tt_content:
            teams_trouter_file.write_text(new_tt, encoding="utf-8")
            print("  ✓ TeamsTrouterListener → 写入/移除 PushNotification routing path")
            print("    效果: TROUTER transport 可生成官方 TFL push context，而不只剩消息上下文")
            print(f"    文件: {teams_trouter_file.relative_to(work_dir)}")
            patch_count += 1

    # === Patch 3: TflRegistrarHelper.getTransportRegistrationArrayForPoll() ===
    # 关键修复: TFL 账户的 EDF 注册不能只上报 MESSAGING，还必须保留 calling 上下文
    print("\n  --- Patch 3: 修复 TFL EDF 上下文注册 ---")

    tfl_registrar_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/longpoll/TflRegistrarHelper"
    )

    if not tfl_registrar_file:
        print("  [ERROR] 未找到 TflRegistrarHelper.smali")
    else:
        tr_content = tfl_registrar_file.read_text(encoding="utf-8")

        method_pattern = (
            r'(\.method public final getTransportRegistrationArrayForPoll'
            r'\(Ljava/lang/String;ILjava/util/Map;'
            r'Lcom/microsoft/teams/core/services/configuration/IUserConfiguration;\)'
            r'\[Lcom/microsoft/skype/teams/data/'
            r'RegistrationNotificationClientSettings\$EdfRegistrationEntry;)'
            r'.*?'
            r'(\.end method)'
        )
        method_replacement = (
            r'\1\n'
            '    .locals 6\n'
            '\n'
            '    # [CHINA-PATCH] TFL 账户保留所有 Trouter contexts，避免只注册 MESSAGING\n'
            '    if-eqz p3, :fallback\n'
            '\n'
            '    invoke-interface {p3}, Ljava/util/Map;->isEmpty()Z\n'
            '\n'
            '    move-result v0\n'
            '\n'
            '    if-nez v0, :fallback\n'
            '\n'
            '    invoke-interface {p3}, Ljava/util/Map;->entrySet()Ljava/util/Set;\n'
            '\n'
            '    move-result-object v0\n'
            '\n'
            '    invoke-interface {v0}, Ljava/util/Set;->size()I\n'
            '\n'
            '    move-result v1\n'
            '\n'
            '    new-array v1, v1, [Lcom/microsoft/skype/teams/data/'
            'RegistrationNotificationClientSettings$EdfRegistrationEntry;\n'
            '\n'
            '    invoke-interface {v0}, Ljava/util/Set;->iterator()Ljava/util/Iterator;\n'
            '\n'
            '    move-result-object v0\n'
            '\n'
            '    const/4 v2, 0x0\n'
            '\n'
            '    :loop_contexts\n'
            '    invoke-interface {v0}, Ljava/util/Iterator;->hasNext()Z\n'
            '\n'
            '    move-result v3\n'
            '\n'
            '    if-eqz v3, :return_contexts\n'
            '\n'
            '    invoke-interface {v0}, Ljava/util/Iterator;->next()Ljava/lang/Object;\n'
            '\n'
            '    move-result-object v3\n'
            '\n'
            '    check-cast v3, Ljava/util/Map$Entry;\n'
            '\n'
            '    new-instance v4, Lcom/microsoft/skype/teams/data/'
            'RegistrationNotificationClientSettings$EdfRegistrationEntry;\n'
            '\n'
            '    invoke-interface {v3}, Ljava/util/Map$Entry;->getKey()Ljava/lang/Object;\n'
            '\n'
            '    move-result-object v5\n'
            '\n'
            '    check-cast v5, Ljava/lang/String;\n'
            '\n'
            '    invoke-interface {v3}, Ljava/util/Map$Entry;->getValue()Ljava/lang/Object;\n'
            '\n'
            '    move-result-object v3\n'
            '\n'
            '    check-cast v3, Ljava/lang/String;\n'
            '\n'
            '    invoke-direct {v4, v5, v3, p2}, Lcom/microsoft/skype/teams/data/'
            'RegistrationNotificationClientSettings$EdfRegistrationEntry;-><init>'
            '(Ljava/lang/String;Ljava/lang/String;I)V\n'
            '\n'
            '    aput-object v4, v1, v2\n'
            '\n'
            '    add-int/lit8 v2, v2, 0x1\n'
            '\n'
            '    goto :loop_contexts\n'
            '\n'
            '    :return_contexts\n'
            '    return-object v1\n'
            '\n'
            '    :fallback\n'
            '    const/4 v0, 0x1\n'
            '\n'
            '    new-array v0, v0, [Lcom/microsoft/skype/teams/data/'
            'RegistrationNotificationClientSettings$EdfRegistrationEntry;\n'
            '\n'
            '    new-instance v1, Lcom/microsoft/skype/teams/data/'
            'RegistrationNotificationClientSettings$EdfRegistrationEntry;\n'
            '\n'
            '    const-string v2, "MESSAGING"\n'
            '\n'
            '    invoke-direct {v1, v2, p1, p2}, Lcom/microsoft/skype/teams/data/'
            'RegistrationNotificationClientSettings$EdfRegistrationEntry;-><init>'
            '(Ljava/lang/String;Ljava/lang/String;I)V\n'
            '\n'
            '    const/4 v2, 0x0\n'
            '\n'
            '    aput-object v1, v0, v2\n'
            '\n'
            '    return-object v0\n'
            r'\2'
        )

        new_tr = re.sub(method_pattern, method_replacement, tr_content, count=1, flags=re.DOTALL)
        if new_tr != tr_content:
            tfl_registrar_file.write_text(new_tr, encoding="utf-8")
            print("  ✓ TflRegistrarHelper.getTransportRegistrationArrayForPoll() → 保留全部 contexts")
            print("    效果: EDF 注册不再硬编码为 [MESSAGING]，可上报 CALLINGEVENTS 等上下文")
            print(f"    文件: {tfl_registrar_file.relative_to(work_dir)}")
            patch_count += 1
        else:
            print("  [ERROR] 未找到 TflRegistrarHelper.getTransportRegistrationArrayForPoll 方法")

    # === Patch 4: 关闭 EDF / notification filter duplicate skip ===
    # 日志显示 TeamsTrouterListener 经常在 registerNotificationFilter 阶段被
    # "Skip registration for duplicate ..." 短路掉，最终报 REGISTRATION_SKIPPED。
    print("\n  --- Patch 4: 禁用 duplicate skip 短路 ---")

    longpoll_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/longpoll/LongPollSyncHelper"
    )

    if not longpoll_file:
        print("  [ERROR] 未找到 LongPollSyncHelper.smali")
    else:
        lp_content = longpoll_file.read_text(encoding="utf-8")

        dup_skip_pattern = re.compile(
            r'(invoke-virtual \{v13, v11, v14, v7, v0\}, '
            r'Lcom/microsoft/skype/teams/services/longpoll/LongPollSyncHelper;'
            r'->notificationSettingDupCheck'
            r'\(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;\)Z\n'
            r'\n'
            r'    move-result v7\n'
            r'\n)'
            r'    if-eqz v7, :cond_2d'
        )

        new_lp = dup_skip_pattern.sub(
            r'\1'
            '    # [CHINA-PATCH] 始终继续注册 notification filter，避免 TeamsTrouterListener 卡在 REGISTRATION_SKIPPED\n'
            '    goto :cond_2d',
            lp_content,
            count=1,
        )

        if new_lp != lp_content:
            longpoll_file.write_text(new_lp, encoding="utf-8")
            print("  ✓ LongPollSyncHelper.registerNotificationFilterForRegistrationId() → 禁用 duplicate skip")
            print("    效果: calling listener 不再因重复设置被短路为 REGISTRATION_SKIPPED")
            print(f"    文件: {longpoll_file.relative_to(work_dir)}")
            patch_count += 1
        else:
            print("  [WARN] 未找到 LongPollSyncHelper 内的 duplicate filter 短路点")

    # LongPollSyncHelper.createEdfRegistration() 的 duplicate notification skip
    # 位于一个独立的 lambda 类中，需要单独 patch。
    lambda15_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/longpoll/LongPollSyncHelper$$ExternalSyntheticLambda15"
    )

    if not lambda15_file:
        print("  [WARN] 未找到 LongPollSyncHelper$$ExternalSyntheticLambda15.smali")
    else:
        l15_content = lambda15_file.read_text(encoding="utf-8")
        dup_notification_pattern = re.compile(
            r'(invoke-virtual \{v10, v12, v8, v0, v1\}, '
            r'Lcom/microsoft/skype/teams/services/longpoll/LongPollSyncHelper;'
            r'->notificationSettingDupCheck'
            r'\(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;\)Z\n'
            r'\n'
            r'    move-result v0\n'
            r'\n)'
            r'    if-eqz v0, :cond_8'
        )
        new_l15 = dup_notification_pattern.sub(
            r'\1'
            '    # [CHINA-PATCH] 始终继续 createEdfRegistration，避免 duplicate notification setting 导致 REGISTRATION_SKIPPED\n'
            '    goto :cond_8',
            l15_content,
            count=1,
        )
        if new_l15 != l15_content:
            lambda15_file.write_text(new_l15, encoding="utf-8")
            print("  ✓ LongPollSyncHelper$$ExternalSyntheticLambda15 → 禁用 duplicate notification skip")
            print("    效果: createEdfRegistration 不再因 duplicate notification setting 直接返回 RegistrationSkipped")
            print(f"    文件: {lambda15_file.relative_to(work_dir)}")
            patch_count += 1
        else:
            print("  [WARN] 未找到 lambda15 内的 duplicate notification 短路点")

    # === Patch 5: AppConfigurationImpl.isChinaPushTransport() = false ===
    # 让 calling/longpoll 相关代码不再继续走 Baidu 的 China push transport 分支。
    print("\n  --- Patch 5: 关闭 China push transport calling 分支 ---")

    app_config_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/services/configuration/AppConfigurationImpl"
    )

    if not app_config_file:
        print("  [ERROR] 未找到 AppConfigurationImpl.smali")
    else:
        ac_content = app_config_file.read_text(encoding="utf-8")
        china_transport_pattern = (
            r'(\.method public final isChinaPushTransport\(\)Z)'
            r'.*?'
            r'(\.end method)'
        )
        china_transport_replacement = (
            r'\1\n'
            '    .locals 1\n'
            '\n'
            '    # [CHINA-PATCH] calling 相关路径按全球版处理，不再视为 China push transport\n'
            '    const/4 v0, 0x0\n'
            '\n'
            '    return v0\n'
            r'\2'
        )
        new_ac = re.sub(
            china_transport_pattern,
            china_transport_replacement,
            ac_content,
            count=1,
            flags=re.DOTALL,
        )
        if new_ac != ac_content:
            app_config_file.write_text(new_ac, encoding="utf-8")
            print("  ✓ AppConfigurationImpl.isChinaPushTransport() → false")
            print("    效果: calling / longpoll 不再沿用 Baidu China push transport 分支")
            print(f"    文件: {app_config_file.relative_to(work_dir)}")
            patch_count += 1
        else:
            print("  [WARN] 未找到 isChinaPushTransport 方法")

    # === Patch 6: Skype endpoint 强制走 push/TFL 分支 ===
    # 避免 skypeMessagePollEnabled=true 时把 Skype endpoint 拉去只注册 MESSAGING。
    print("\n  --- Patch 6: 强制 Skype endpoint 走 TFL push 分支 ---")

    if not longpoll_file:
        print("  [ERROR] 未找到 LongPollSyncHelper.smali")
    else:
        lp_content = longpoll_file.read_text(encoding="utf-8")
        skype_poll_pattern = re.compile(
            r'(const-string/jumbo v4, "skypeMessagePollEnabled"\n'
            r'\n'
            r'    invoke-interface \{v12, v4\}, '
            r'Lcom/microsoft/teams/nativecore/INativeCoreExperimentationManager;'
            r'->getEcsSettingAsBoolean\(Ljava/lang/String;\)Z\n'
            r'\n'
            r'    move-result v4\n'
            r'\n)'
            r'    if-eqz v4, :cond_17'
        )
        new_lp = skype_poll_pattern.sub(
            r'\1'
            '    # [CHINA-PATCH] 无论 ECS 如何，Skype endpoint 都走 push/TFL 分支\n'
            '    goto :cond_17',
            lp_content,
            count=1,
        )
        if new_lp != lp_content:
            longpoll_file.write_text(new_lp, encoding="utf-8")
            print("  ✓ LongPollSyncHelper → Skype endpoint 强制走 push/TFL 分支")
            print("    效果: Skype endpoint 的 TROUTER context 不再被 ECS 拉回仅 MESSAGING")
            print(f"    文件: {longpoll_file.relative_to(work_dir)}")
            patch_count += 1
        else:
            print("  [WARN] 未找到 skypeMessagePollEnabled 分支")

    # === Patch 7: mPrematureNotificationFlowEnabled = true ===
    # 辅助修复: 应用在后台时通过推送来电通知唤醒
    print("\n  --- Patch 7: 启用 Premature Notification Flow ---")

    call_manager_file = find_smali_file(
        work_dir,
        "com/microsoft/skype/teams/calling/call/CallManager"
    )

    if not call_manager_file:
        print("  [ERROR] 未找到 CallManager.smali")
    else:
        cm_content = call_manager_file.read_text(encoding="utf-8")

        # 找到 mPrematureNotificationFlowEnabled 的 iput-boolean 赋值
        iput_pattern = re.compile(
            r'(    iput-boolean (v\d+), v\d+, '
            r'Lcom/microsoft/skype/teams/calling/call/CallManager;'
            r'->mPrematureNotificationFlowEnabled:Z)'
        )

        match = iput_pattern.search(cm_content)
        if not match:
            print("  [ERROR] 未找到 mPrematureNotificationFlowEnabled 赋值位置")
        else:
            reg = match.group(2)
            full_line = match.group(1)

            inject = (
                f'    # [CHINA-PATCH] 强制启用 Premature Notification Flow\n'
                f'    const/4 {reg}, 0x1\n'
                f'\n'
                f'{full_line}'
            )
            new_cm = cm_content.replace(full_line, inject, 1)
            if new_cm != cm_content:
                call_manager_file.write_text(new_cm, encoding="utf-8")
                print(f"  ✓ CallManager.<init>: mPrematureNotificationFlowEnabled = true")
                print(f"    效果: 来电通知将在 SkyLib 初始化前立即显示")
                print(f"    文件: {call_manager_file.relative_to(work_dir)}")
                patch_count += 1
            else:
                print("  [ERROR] mPrematureNotificationFlowEnabled 替换未生效")

    return patch_count


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
                        help="修复个人账户来电接收 (启用 Calling Trouter/TFL EDF/Premature Flow)")
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

        # Step 2c: Patch TFL 登录后链路 (新版完整性/License 问题)
        patch_tfl_post_login_chain(work_dir)

        # Step 2d: 可选 — 跳过弹窗
        if args.skip_dialogs:
            patch_auto_skip_dialogs(work_dir)

        # Step 2e: 可选 — 修复来电接收
        if args.fix_incoming_calls:
            patch_fix_incoming_calls(work_dir)

        # Step 2f: 可选 — 裁剪架构
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
