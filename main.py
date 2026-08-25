def _merge_template(template, data):
    """Merge proxies and proxy-groups into the template."""
    proxies = data["proxies"]
    proxies.sort(key=lambda p: p.get("name", ""))

    # Fix reality-opts short-id
    _fix_reality_short_id(proxies)
    # 新增：统一修复 http-opts.headers 格式问题
    _fix_http_headers_host_slice(proxies)

    template["proxies"] = proxies

    proxy_groups = data.get("proxy-groups")
    
    # 检查是否已存在"♻️ 自动选择"分组
    has_auto_select = proxy_groups and any(g.get("name") == "♻️ 自动选择" for g in proxy_groups)
    
    if proxy_groups:
        names_to_remove = {p["name"] for p in proxy_groups}
        template["proxy-groups"] = [
            g for g in template["proxy-groups"] if g["name"] not in names_to_remove
        ]
        for g in template["proxy-groups"]:
            if g["name"] in ("🚀 节点选择", "🌍 国外媒体", "📲 电报信息", "Ⓜ️ 微软服务", "🍎 苹果服务"):
                g["proxies"].extend(p["name"] for p in proxy_groups)
        template["proxy-groups"].extend(proxy_groups)
        
        # 如果订阅分组中没有"♻️ 自动选择"，则创建并添加
        if not has_auto_select:
            auto_group = {
                "name": "♻️ 自动选择",
                "type": "url-test",
                "proxies": [p["name"] for p in proxies],
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "tolerance": 50,
            }
            template["proxy-groups"].append(auto_group)
            # 在相关分组中引用新创建的自动选择
            for g in template["proxy-groups"]:
                if g["name"] in ("🌍 国外媒体", "📲 电报信息", "Ⓜ️ 微软服务", "🍎 苹果服务"):
                    if "♻️ 自动选择" not in g["proxies"]:
                        g["proxies"].append("♻️ 自动选择")
    else:
        # 没有订阅分组时，创建自动选择分组
        auto_group = {
            "name": "♻️ 自动选择",
            "type": "url-test",
            "proxies": [p["name"] for p in proxies],
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        }
        template["proxy-groups"].append(auto_group)
        for g in template["proxy-groups"]:
            if g["name"] == "🚀 节点选择":
                g["proxies"].extend(p["name"] for p in proxies)
            if g["name"] in ("🌍 国外媒体", "📲 电报信息", "Ⓜ️ 微软服务", "🍎 苹果服务"):
                g["proxies"].append("♻️ 自动选择")

    return template
