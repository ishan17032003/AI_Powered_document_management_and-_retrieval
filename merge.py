import yaml

with open('docker-compose.yml', 'r') as f:
    main_compose = yaml.safe_load(f)

with open('docker-compose.override.yml', 'r') as f:
    dev_compose = yaml.safe_load(f)

for service_name, dev_config in dev_compose.get('services', {}).items():
    if service_name in main_compose['services']:
        new_service_name = f"{service_name}-dev"
        
        # We start with the base config or extend it
        main_config = dict(main_compose['services'][service_name])
        
        # Merge dev_config into main_config
        # This is a naive merge for dicts and lists
        for k, v in dev_config.items():
            if isinstance(v, dict) and k in main_config and isinstance(main_config[k], dict):
                main_config[k].update(v)
            elif isinstance(v, list) and k in main_config and isinstance(main_config[k], list):
                # deduplicate lists if needed, or just append
                main_config[k].extend(x for x in v if x not in main_config[k])
            else:
                main_config[k] = v
                
        main_config['profiles'] = ["development"]
        
        # Add to main
        main_compose['services'][new_service_name] = main_config

# Remove qdrant if it's there
if 'qdrant' in dev_compose.get('services', {}):
    qdrant_config = dev_compose['services']['qdrant']
    qdrant_config['profiles'] = ["development"]
    main_compose['services']['qdrant-dev'] = qdrant_config

with open('docker-compose.new.yml', 'w') as f:
    yaml.dump(main_compose, f, sort_keys=False)

