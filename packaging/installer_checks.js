// Installer JavaScript: both component packages share this exclusive selection.
function readHardware(selector) {
    try { return system.sysctl(selector); } catch (error) { return null; }
}

function targetArchitecture() {
    var arm = readHardware('hw.optional.arm64');
    var translated = readHardware('sysctl.proc_translated');
    var machine = String(readHardware('hw.machine') || '');
    if (arm == 1 || translated == 1 || machine == 'arm64' || machine == 'arm64e') {
        return 'arm64';
    }
    if (machine == 'x86_64' && (arm == 0 || arm === null) &&
            (translated == 0 || translated === null)) {
        return 'x86_64';
    }
    return '';
}

function checkRequirements() {
    if (system.compareVersions(system.version.ProductVersion, '12.0') < 0) {
        my.result.type = 'Fatal';
        my.result.title = '系统版本不支持';
        my.result.message = '中犇微信助手需要 macOS 12.0 或更新版本。';
        return false;
    }
    if (targetArchitecture() === '') {
        my.result.type = 'Fatal';
        my.result.title = '无法识别电脑芯片';
        my.result.message = '请使用 Apple 芯片或 Intel 64 位 Mac。';
        return false;
    }
    return true;
}
