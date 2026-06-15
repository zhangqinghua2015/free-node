def _fix_reality_short_id(proxies):
    """Fix reality-opts short-id: add single quotes if missing."""
    for proxy in proxies:
        if isinstance(proxy, dict) and "reality-opts" in proxy:
            reality_opts = proxy["reality-opts"]
            if isinstance(reality_opts, dict) and "short-id" in reality_opts:
                short_id = reality_opts["short-id"]
                # Check if short-id is not already quoted (e.g., string wrapped in quotes)
                # If it's a plain value without quotes, wrap it
                if isinstance(short_id, str) and not (short_id.startswith("'") and short_id.endswith("'")):
                    # Use a custom YAML representer to output with literal block or quoted scalar
                    from yaml.representer import SafeRepresenter
                    # Create a custom string type that forces quoting in YAML output
                    class QuotedString(str):
                        pass
                    
                    def represent_quoted_string(dumper, data):
                        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style="'")
                    
                    # Register the custom representer (do this once per session)
                    if not hasattr(yaml, '_quoted_string_representer_set'):
                        yaml.add_representer(QuotedString, represent_quoted_string)
                        yaml._quoted_string_representer_set = True
                    
                    reality_opts["short-id"] = QuotedString(short_id)
                    print(f"[INFO] Fixed reality-opts short-id for proxy '{proxy.get('name', 'unknown')}': {short_id} -> '{short_id}'")
