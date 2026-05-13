def extract_tenant_org(all_orgs, tenant_id):
    for org in all_orgs:
        if not org.get("attributes"):
            continue

        if not org["attributes"].get("tenant ID"):
            continue

        if len(org["attributes"]["tenant ID"]) <=0 :
            continue

        if not org["attributes"]["tenant ID"][0] == tenant_id:
            continue

        return org

    return None

def extract_tenant_from_tenants(tenants, tenant_name):
    for tenant in tenants:
        if not tenant.get("name"):
            continue

        if not tenant["name"] == tenant_name:
            continue

        return tenant

    return None

def extract_user_from_users(users, user_email):
    for user in users:
        if not user.get("email"):
            continue

        if not user["email"] == user_email:
            continue

        return user

    return None
