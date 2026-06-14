
# Watchdog Bot

The **Watchdog Bot** is a Discord bot designed to provide quick and easy removal of spam bots.

## Installation

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/metriccepheid/nhxinfobot.git -b watchdog watchdog
   cd watchdog
   ```
The extra watchdog at the end of the command ensures it clones into a folder named "watchdog"

2. **Install Dependencies**:
   Ensure you have Python installed. Install the required Python packages using pip:
   ```bash
   pip install discord.py
   ```

3. **Configuration**:
   - Create a `config.json` file in the root directory of the project with the following structure:
     ```json
     {
         "bot_token": "YOUR_DISCORD_BOT_TOKEN"
     }
     ```

   - Make sure that you edit the `watchdog.py` file
     - Go to line 17 and make sure the Discord channel ID is set to a channel where you want alerts anytime the Watchdog goes off about a spammer
     - Go to line 110 and make sure the Discord channel ID is set to a channel that is set up as an announcement channel otherwise the program will error

4. **Run the Bot**:
   Start the bot by running:
   ```bash
   python watchdog.py
   ```

## Contributing

Contributions are welcome! If you have ideas for additional improvements to the bot, feel free to open a pull request or submit an issue.