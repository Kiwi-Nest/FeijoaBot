import discord

from modules.dtypes import GuildId
from modules.UserDB import UserDB


class InvalidRoleError(Exception):
    pass


async def ping_online_role(role: discord.Role, user_db: UserDB) -> str | None:
    """Find all active online members with a role and return string to ping them.

    Args:
    ----
        role (discord.Role): The role to ping. The user can pass the role's name, ID, or mention.
        user_db (UserDB): UserDB instance

    """
    MAX_USER = 30

    if role.is_default():
        raise InvalidRoleError

    # Get a set of user IDs that have been active within the last 7 days.
    active_users_ids = set(await user_db.get_active_users(GuildId(role.guild.id), 7))

    # Prioritize online members who are also active.
    online_active_members = [
        member for member in role.members if member.status != discord.Status.offline and member.id in active_users_ids
    ]

    if online_active_members and len(online_active_members) < MAX_USER:
        return " ".join(member.mention for member in online_active_members)

    # If no online active members, fallback to pinging all active members.
    active_members = [member for member in role.members if member.id in active_users_ids]

    if active_members and len(active_members) < MAX_USER:
        return " ".join(member.mention for member in active_members)

    # If all other conditions fail, fall back to mentioning the entire role.
    return role.mention
