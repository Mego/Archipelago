def get_rules(player, fishing):
    return {
        "Old One's Army Tier 1": lambda state: state.has("Post-Eater of Worlds or Brain of Cthulhu", player),
        "Pirate Invasion": lambda state: state.has("Hardmode", player),
        "Queen Slime": lambda state: state.has("Hardmode", player),
        "The Twins": lambda state: state.has("Hardmode", player),
        "The Destroyer": lambda state: state.has("Hardmode", player),
        "Skeletron Prime": lambda state: state.has_all({"Post-Skeletron", "Hardmode"}, player),
        "Old One's Army Tier 2": lambda state: state.has_any({"Post-The Twins", "Post-The Destroyer", "Post-Skeletron Prime", "Post-Golem"}, player) and state.has_all({"Post-Eater of Worlds or Brain of Cthulhu", "Hardmode"}, player),
        "Plantera": lambda state: state.has_all({"Hardmode", "Post-The Twins", "Post-The Destroyer", "Post-Skeletron Prime"}, player),
        "Duke Fishron": lambda state: state.has("Hardmode", player),
        "Frost Legion": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Golem": lambda state: (state.has_all({"Post-The Twins", "Post-The Destroyer", "Post-Skeletron Prime"}, player) or state.has("Post-Golem", player)) and state.has_all({"Hardmode", "Post-Plantera"}, player),
        "Old One's Army Tier 3": lambda state: state.has_all({"Post-Eater of Worlds or Brain of Cthulhu", "Hardmode", "Post-Golem"}, player),
        "Martian Madness": lambda state: state.has_all({"Hardmode", "Post-Golem"}, player),
        "Mourning Wood": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Pumpking": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Everscream": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Santa-NK1": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Ice Queen": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Empress of Light": lambda state: state.has_all({"Hardmode", "Post-Plantera"}, player),
        "Lunatic Cultist": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Golem"}, player),
        "Lunar Events": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Golem"}, player),
        "Moon Lord": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Golem"}, player),
        "Zenith": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-The Twins", "Post-The Destroyer", "Post-Skeletron Prime", "Post-Plantera", "Post-Golem"}, player),
        "Dungeon Heist": lambda state: state.has("Post-Skeletron", player),
        "Boots of the Hero": lambda state: state.has("Post-Goblin Army", player),
        "Head in the Clouds": lambda state: fishing or state.has("Hardmode", player),
        "Begone, Evil!": lambda state: state.has("Hardmode", player),
        "Extra Shiny!": lambda state: state.has("Hardmode", player),
        "Drax Attax": lambda state: state.has_all({"Post-Skeletron", "Hardmode"}, player),
        "Photosynthesis": lambda state: state.has_all({"Post-Skeletron", "Hardmode"}, player),
        "Get a Life": lambda state: state.has("Hardmode", player),
        "Kill the Sun": lambda state: state.has("Hardmode", player),
        "Mecha Mayhem": lambda state: state.has_all({"Post-Skeletron", "Hardmode"}, player),
        "Prismancer": lambda state: state.has("Hardmode", player),
        "It Can Talk?!": lambda state: state.has("Hardmode", player),
        "Gelatin World Tour": lambda state: state.has_all({"Post-Skeletron", "Hardmode"}, player),
        "Topped Off": lambda state: state.has("Hardmode", player),
        "Don't Dread on Me": lambda state: state.has("Hardmode", player),
        "Temple Raider": lambda state: state.has_all({"Hardmode", "Post-The Twins", "Post-The Destroyer", "Post-Skeletron Prime"}, player),
        "Robbing the Grave": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Baleful Harvest": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Ice Scream": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Sword of the Hero": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Big Booty": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Real Estate Agent": lambda state: state.has_all({"Post-Goblin Army", "Post-Eater of Worlds or Brain of Cthulhu", "Post-Queen Bee", "Post-Skeletron", "Hardmode", "Post-Pirate Invasion", "Post-Plantera"}, player) and state.has_any({"Post-The Twins", "Post-The Destroyer", "Post-Skeletron Prime"}, player),
        "Rainbows and Unicorns": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Plantera"}, player),
        "Sick Throw": lambda state: state.has_all({"Post-Skeletron", "Hardmode", "Post-Golem"}, player),
        "You and What Army?": lambda state: state.has_any({"Post-Queen Bee", "Post-Plantera"}, player) and state.has_all({"Post-Skeletron", "Hardmode", "Post-Golem"}, player),
    }
