from typing import Optional
import datetime
import os
import signal
import subprocess
import sys
import typer
from pathlib import Path
from functools import wraps
from rich.console import Console
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.columns import Columns
from rich.markdown import Markdown
from rich.layout import Layout
from rich.text import Text
from rich.table import Table
from collections import deque
import time
from rich.tree import Tree
from rich import box
from rich.align import Align
from rich.rule import Rule

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.backtest.replay import ReplayRunner
from tradingagents.brokers.alpaca import AlpacaPaperBroker, BrokerConfigurationError
from tradingagents.dashboard import build_dashboard_data_service
from tradingagents.dashboard.server import DashboardServer
from tradingagents.daemon.service import DaemonService
from tradingagents.execution.config import load_execution_config, load_risk_config
from tradingagents.execution.models import RunMode
from tradingagents.orchestration.runner import TradingAgentsAnalysisEngine, TradingCycleRunner
from tradingagents.persistence.sqlite_store import SQLitePersistence
from cli.models import AnalystType
from cli.utils import *
from cli.announcements import fetch_announcements, display_announcements
from cli.stats_handler import StatsCallbackHandler

console = Console()

app = typer.Typer(
    name="TradingBot",
    help="TradingBot CLI: autonomous paper-trading system inspired by TradingAgents",
    add_completion=True,  # Enable shell completion
    invoke_without_command=True,
)
daemon_app = typer.Typer(
    help="Long-running paper-trading automation service",
)
app.add_typer(daemon_app, name="daemon")
dashboard_app = typer.Typer(
    help="Web dashboard for monitoring the paper-trading daemon",
)
app.add_typer(dashboard_app, name="dashboard")


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    # Fixed teams that always run (not user-selectable)
    FIXED_AGENTS = {
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Analyst name mapping
    ANALYST_MAPPING = {
        "market": "Market Analyst",
        "social": "Social Analyst",
        "news": "News Analyst",
        "fundamentals": "Fundamentals Analyst",
    }

    # Report section mapping: section -> (analyst_key for filtering, finalizing_agent)
    # analyst_key: which analyst selection controls this section (None = always included)
    # finalizing_agent: which agent must be "completed" for this report to count as done
    REPORT_SECTIONS = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Social Analyst"),
        "news_report": ("news", "News Analyst"),
        "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
    }

    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {}
        self.current_agent = None
        self.report_sections = {}
        self.selected_analysts = []
        self._last_message_id = None

    def init_for_analysis(self, selected_analysts):
        """Initialize agent status and report sections based on selected analysts.

        Args:
            selected_analysts: List of analyst type strings (e.g., ["market", "news"])
        """
        self.selected_analysts = [a.lower() for a in selected_analysts]

        # Build agent_status dynamically
        self.agent_status = {}

        # Add selected analysts
        for analyst_key in self.selected_analysts:
            if analyst_key in self.ANALYST_MAPPING:
                self.agent_status[self.ANALYST_MAPPING[analyst_key]] = "pending"

        # Add fixed teams
        for team_agents in self.FIXED_AGENTS.values():
            for agent in team_agents:
                self.agent_status[agent] = "pending"

        # Build report_sections dynamically
        self.report_sections = {}
        for section, (analyst_key, _) in self.REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected_analysts:
                self.report_sections[section] = None

        # Reset other state
        self.current_report = None
        self.final_report = None
        self.current_agent = None
        self.messages.clear()
        self.tool_calls.clear()
        self._last_message_id = None

    def get_completed_reports_count(self):
        """Count reports that are finalized (their finalizing agent is completed).

        A report is considered complete when:
        1. The report section has content (not None), AND
        2. The agent responsible for finalizing that report has status "completed"

        This prevents interim updates (like debate rounds) from counting as completed.
        """
        count = 0
        for section in self.report_sections:
            if section not in self.REPORT_SECTIONS:
                continue
            _, finalizing_agent = self.REPORT_SECTIONS[section]
            # Report is complete if it has content AND its finalizing agent is done
            has_content = self.report_sections.get(section) is not None
            agent_done = self.agent_status.get(finalizing_agent) == "completed"
            if has_content and agent_done:
                count += 1
        return count

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content
               
        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "fundamentals_report": "Fundamentals Analysis",
                "investment_plan": "Research Team Decision",
                "trader_investment_plan": "Trading Team Plan",
                "final_trade_decision": "Portfolio Management Decision",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports - use .get() to handle missing sections
        analyst_sections = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
        if any(self.report_sections.get(section) for section in analyst_sections):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections.get("market_report"):
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections.get("sentiment_report"):
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections.get("news_report"):
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )
            if self.report_sections.get("fundamentals_report"):
                report_parts.append(
                    f"### Fundamentals Analysis\n{self.report_sections['fundamentals_report']}"
                )

        # Research Team Reports
        if self.report_sections.get("investment_plan"):
            report_parts.append("## Research Team Decision")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections.get("trader_investment_plan"):
            report_parts.append("## Trading Team Plan")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Portfolio Management Decision
        if self.report_sections.get("final_trade_decision"):
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def format_tokens(n):
    """Format token count for display."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def update_display(layout, spinner_text=None, stats_handler=None, start_time=None):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to TradingBot CLI[/bold green]\n"
            "[dim]Inspired by TradingAgents and extended for autonomous paper trading[/dim]",
            title="Welcome to TradingBot",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team - filter to only include agents in agent_status
    all_teams = {
        "Analyst Team": [
            "Market Analyst",
            "Social Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Filter teams to only include agents that are in agent_status
    teams = {}
    for team, agents in all_teams.items():
        active_agents = [a for a in agents if a in message_buffer.agent_status]
        if active_agents:
            teams[team] = active_agents

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status.get(first_agent, "pending")
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status.get(agent, "pending")
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        formatted_args = format_tool_args(args)
        all_messages.append((timestamp, "Tool", f"{tool_name}: {formatted_args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        content_str = str(content) if content else ""
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # Sort by timestamp descending (newest first)
    all_messages.sort(key=lambda x: x[0], reverse=True)

    # Calculate how many messages we can show based on available space
    max_messages = 12

    # Get the first N messages (newest ones)
    recent_messages = all_messages[:max_messages]

    # Add messages to table (already in newest-first order)
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]Waiting for analysis report...[/italic]",
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )

    # Footer with statistics
    # Agent progress - derived from agent_status dict
    agents_completed = sum(
        1 for status in message_buffer.agent_status.values() if status == "completed"
    )
    agents_total = len(message_buffer.agent_status)

    # Report progress - based on agent completion (not just content existence)
    reports_completed = message_buffer.get_completed_reports_count()
    reports_total = len(message_buffer.report_sections)

    # Build stats parts
    stats_parts = [f"Agents: {agents_completed}/{agents_total}"]

    # LLM and tool stats from callback handler
    if stats_handler:
        stats = stats_handler.get_stats()
        stats_parts.append(f"LLM: {stats['llm_calls']}")
        stats_parts.append(f"Tools: {stats['tool_calls']}")

        # Token display with graceful fallback
        if stats["tokens_in"] > 0 or stats["tokens_out"] > 0:
            tokens_str = f"Tokens: {format_tokens(stats['tokens_in'])}\u2191 {format_tokens(stats['tokens_out'])}\u2193"
        else:
            tokens_str = "Tokens: --"
        stats_parts.append(tokens_str)

    stats_parts.append(f"Reports: {reports_completed}/{reports_total}")

    # Elapsed time
    if start_time:
        elapsed = time.time() - start_time
        elapsed_str = f"\u23f1 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        stats_parts.append(elapsed_str)

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(" | ".join(stats_parts))

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """Get all user selections before starting the analysis display."""
    # Display ASCII art welcome message
    with open(Path(__file__).parent / "static" / "welcome.txt", "r") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingBot: Autonomous Paper-Trading System[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Inspired by TradingAgents and extended with paper execution, daemon automation, and monitoring[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to TradingBot",
        subtitle="Autonomous paper trading inspired by TradingAgents",
    )
    console.print(Align.center(welcome_box))
    console.print()
    console.print()  # Add vertical space before announcements

    # Fetch and display announcements (silent on failure)
    announcements = fetch_announcements()
    display_announcements(console, announcements)

    # Create a boxed questionnaire for each step
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]Default: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol",
            "Enter the exact ticker symbol to analyze, including exchange suffix when needed (examples: SPY, CNC.TO, 7203.T, 0700.HK)",
            "SPY",
        )
    )
    selected_ticker = get_ticker()

    # Step 2: Analysis date
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "Step 2: Analysis Date",
            "Enter the analysis date (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date()

    # Step 3: Output language
    console.print(
        create_question_box(
            "Step 3: Output Language",
            "Select the language for analyst reports and final decision"
        )
    )
    output_language = ask_output_language()

    # Step 4: Select analysts
    console.print(
        create_question_box(
            "Step 4: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts()
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 5: Research depth
    console.print(
        create_question_box(
            "Step 5: Research Depth", "Select your research depth level"
        )
    )
    selected_research_depth = select_research_depth()

    # Step 6: LLM Provider
    console.print(
        create_question_box(
            "Step 6: LLM Provider", "Select your LLM provider"
        )
    )
    selected_llm_provider, backend_url = select_llm_provider()

    # Step 7: Thinking agents
    console.print(
        create_question_box(
            "Step 7: Thinking Agents", "Select your thinking agents for analysis"
        )
    )
    selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
    selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # Step 8: Provider-specific thinking configuration
    thinking_level = None
    reasoning_effort = None
    anthropic_effort = None

    provider_lower = selected_llm_provider.lower()
    if provider_lower == "google":
        console.print(
            create_question_box(
                "Step 8: Thinking Mode",
                "Configure Gemini thinking mode"
            )
        )
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(
            create_question_box(
                "Step 8: Reasoning Effort",
                "Configure OpenAI reasoning effort level"
            )
        )
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(
            create_question_box(
                "Step 8: Effort Level",
                "Configure Claude effort level"
            )
        )
        anthropic_effort = ask_anthropic_effort()

    return {
        "ticker": selected_ticker,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "google_thinking_level": thinking_level,
        "openai_reasoning_effort": reasoning_effort,
        "anthropic_effort": anthropic_effort,
        "output_language": output_language,
    }


def get_ticker():
    """Get ticker symbol from user input."""
    return typer.prompt("", default="SPY")


def get_analysis_date():
    """Get the analysis date from user input."""
    while True:
        date_str = typer.prompt(
            "", default=datetime.datetime.now().strftime("%Y-%m-%d")
        )
        try:
            # Validate date format and ensure it's not in the future
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > datetime.datetime.now().date():
                console.print("[red]Error: Analysis date cannot be in the future[/red]")
                continue
            return date_str
        except ValueError:
            console.print(
                "[red]Error: Invalid date format. Please use YYYY-MM-DD[/red]"
            )


def save_report_to_disk(final_state, ticker: str, save_path: Path):
    """Save complete analysis report to disk with organized subfolders."""
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []

    # 1. Analysts
    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "market.md").write_text(final_state["market_report"])
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "sentiment.md").write_text(final_state["sentiment_report"])
        analyst_parts.append(("Social Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "news.md").write_text(final_state["news_report"])
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "fundamentals.md").write_text(final_state["fundamentals_report"])
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    # 2. Research
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bull.md").write_text(debate["bull_history"])
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bear.md").write_text(debate["bear_history"])
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "manager.md").write_text(debate["judge_decision"])
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    # 3. Trading
    if final_state.get("trader_investment_plan"):
        trading_dir = save_path / "3_trading"
        trading_dir.mkdir(exist_ok=True)
        (trading_dir / "trader.md").write_text(final_state["trader_investment_plan"])
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{final_state['trader_investment_plan']}")

    # 4. Risk Management
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "aggressive.md").write_text(risk["aggressive_history"])
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "conservative.md").write_text(risk["conservative_history"])
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "neutral.md").write_text(risk["neutral_history"])
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        # 5. Portfolio Manager
        if risk.get("judge_decision"):
            portfolio_dir = save_path / "5_portfolio"
            portfolio_dir.mkdir(exist_ok=True)
            (portfolio_dir / "decision.md").write_text(risk["judge_decision"])
            sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}")

    # Write consolidated report
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    (save_path / "complete_report.md").write_text(header + "\n\n".join(sections))
    return save_path / "complete_report.md"


def display_complete_report(final_state):
    """Display the complete analysis report sequentially (avoids truncation)."""
    console.print()
    console.print(Rule("Complete Analysis Report", style="bold green"))

    # I. Analyst Team Reports
    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Social Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analysts:
        console.print(Panel("[bold]I. Analyst Team Reports[/bold]", border_style="cyan"))
        for title, content in analysts:
            console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research.append(("Research Manager", debate["judge_decision"]))
        if research:
            console.print(Panel("[bold]II. Research Team Decision[/bold]", border_style="magenta"))
            for title, content in research:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # III. Trading Team
    if final_state.get("trader_investment_plan"):
        console.print(Panel("[bold]III. Trading Team Plan[/bold]", border_style="yellow"))
        console.print(Panel(Markdown(final_state["trader_investment_plan"]), title="Trader", border_style="blue", padding=(1, 2)))

    # IV. Risk Management Team
    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            console.print(Panel("[bold]IV. Risk Management Team Decision[/bold]", border_style="red"))
            for title, content in risk_reports:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

        # V. Portfolio Manager Decision
        if risk.get("judge_decision"):
            console.print(Panel("[bold]V. Portfolio Manager Decision[/bold]", border_style="green"))
            console.print(Panel(Markdown(risk["judge_decision"]), title="Portfolio Manager", border_style="blue", padding=(1, 2)))


def update_research_team_status(status):
    """Update status for research team members (not Trader)."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


# Ordered list of analysts for status transitions
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def update_analyst_statuses(message_buffer, chunk):
    """Update analyst statuses based on accumulated report state.

    Logic:
    - Store new report content from the current chunk if present
    - Check accumulated report_sections (not just current chunk) for status
    - Analysts with reports = completed
    - First analyst without report = in_progress
    - Remaining analysts without reports = pending
    - When all analysts done, set Bull Researcher to in_progress
    """
    selected = message_buffer.selected_analysts
    found_active = False

    for analyst_key in ANALYST_ORDER:
        if analyst_key not in selected:
            continue

        agent_name = ANALYST_AGENT_NAMES[analyst_key]
        report_key = ANALYST_REPORT_MAP[analyst_key]

        # Capture new report content from current chunk
        if chunk.get(report_key):
            message_buffer.update_report_section(report_key, chunk[report_key])

        # Determine status from accumulated sections, not just current chunk
        has_report = bool(message_buffer.report_sections.get(report_key))

        if has_report:
            message_buffer.update_agent_status(agent_name, "completed")
        elif not found_active:
            message_buffer.update_agent_status(agent_name, "in_progress")
            found_active = True
        else:
            message_buffer.update_agent_status(agent_name, "pending")

    # When all analysts complete, transition research team to in_progress
    if not found_active and selected:
        if message_buffer.agent_status.get("Bull Researcher") == "pending":
            message_buffer.update_agent_status("Bull Researcher", "in_progress")

def extract_content_string(content):
    """Extract string content from various message formats.
    Returns None if no meaningful text content is found.
    """
    import ast

    def is_empty(val):
        """Check if value is empty using Python's truthiness."""
        if val is None or val == '':
            return True
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return True
            try:
                return not bool(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                return False  # Can't parse = real text
        return not bool(val)

    if is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not is_empty(t))
        return result if result else None

    return str(content).strip() if not is_empty(content) else None


def classify_message_type(message) -> tuple[str, str | None]:
    """Classify LangChain message into display type and extract content.

    Returns:
        (type, content) - type is one of: User, Agent, Data, Control
                        - content is extracted string or None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        return ("Agent", content)

    # Fallback for unknown types
    return ("System", content)


def format_tool_args(args, max_length=80) -> str:
    """Format tool arguments for terminal display."""
    result = str(args)
    if len(result) > max_length:
        return result[:max_length - 3] + "..."
    return result

def run_analysis():
    # First get all user selections
    selections = get_user_selections()

    # Create config with selected research depth
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = selections["research_depth"]
    config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # Provider-specific thinking configuration
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "English")

    # Create stats callback handler for tracking LLM/tool calls
    stats_handler = StatsCallbackHandler()

    # Normalize analyst selection to predefined order (selection is a 'set', order is fixed)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]

    # Initialize the graph with callbacks bound to LLMs
    graph = TradingAgentsGraph(
        selected_analyst_keys,
        config=config,
        debug=True,
        callbacks=[stats_handler],
    )

    # Initialize message buffer with selected analysts
    message_buffer.init_for_analysis(selected_analyst_keys)

    # Track start time for elapsed display
    start_time = time.time()

    # Create result directory
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # Replace newlines with spaces
            with open(log_file, "a") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper
    
    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    text = "\n".join(str(item) for item in content) if isinstance(content, list) else content
                    with open(report_dir / file_name, "w") as f:
                        f.write(text)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    # Now start the display layout
    layout = create_layout()

    with Live(layout, refresh_per_second=4) as live:
        # Initial display
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Update agent status to in_progress for the first analyst
        first_analyst = f"{selections['analysts'][0].value.capitalize()} Analyst"
        message_buffer.update_agent_status(first_analyst, "in_progress")
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Create spinner text
        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text, stats_handler=stats_handler, start_time=start_time)

        # Initialize state and get graph args with callbacks
        init_agent_state = graph.propagator.create_initial_state(
            selections["ticker"], selections["analysis_date"]
        )
        # Pass callbacks to graph config for tool execution tracking
        # (LLM tracking is handled separately via LLM constructor)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        # Stream the analysis
        trace = []
        for chunk in graph.graph.stream(init_agent_state, **args):
            # Process messages if present (skip duplicates via message ID)
            if len(chunk["messages"]) > 0:
                last_message = chunk["messages"][-1]
                msg_id = getattr(last_message, "id", None)

                if msg_id != message_buffer._last_message_id:
                    message_buffer._last_message_id = msg_id

                    # Add message to buffer
                    msg_type, content = classify_message_type(last_message)
                    if content and content.strip():
                        message_buffer.add_message(msg_type, content)

                    # Handle tool calls
                    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                        for tool_call in last_message.tool_calls:
                            if isinstance(tool_call, dict):
                                message_buffer.add_tool_call(
                                    tool_call["name"], tool_call["args"]
                                )
                            else:
                                message_buffer.add_tool_call(tool_call.name, tool_call.args)

            # Update analyst statuses based on report state (runs on every chunk)
            update_analyst_statuses(message_buffer, chunk)

            # Research Team - Handle Investment Debate State
            if chunk.get("investment_debate_state"):
                debate_state = chunk["investment_debate_state"]
                bull_hist = debate_state.get("bull_history", "").strip()
                bear_hist = debate_state.get("bear_history", "").strip()
                judge = debate_state.get("judge_decision", "").strip()

                # Only update status when there's actual content
                if bull_hist or bear_hist:
                    update_research_team_status("in_progress")
                if bull_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bull Researcher Analysis\n{bull_hist}"
                    )
                if bear_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bear Researcher Analysis\n{bear_hist}"
                    )
                if judge:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Research Manager Decision\n{judge}"
                    )
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            # Trading Team
            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section(
                    "trader_investment_plan", chunk["trader_investment_plan"]
                )
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            # Risk Management Team - Handle Risk Debate State
            if chunk.get("risk_debate_state"):
                risk_state = chunk["risk_debate_state"]
                agg_hist = risk_state.get("aggressive_history", "").strip()
                con_hist = risk_state.get("conservative_history", "").strip()
                neu_hist = risk_state.get("neutral_history", "").strip()
                judge = risk_state.get("judge_decision", "").strip()

                if agg_hist:
                    if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Aggressive Analyst Analysis\n{agg_hist}"
                    )
                if con_hist:
                    if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                        message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Conservative Analyst Analysis\n{con_hist}"
                    )
                if neu_hist:
                    if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                        message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Neutral Analyst Analysis\n{neu_hist}"
                    )
                if judge:
                    if message_buffer.agent_status.get("Portfolio Manager") != "completed":
                        message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Portfolio Manager Decision\n{judge}"
                        )
                        message_buffer.update_agent_status("Aggressive Analyst", "completed")
                        message_buffer.update_agent_status("Conservative Analyst", "completed")
                        message_buffer.update_agent_status("Neutral Analyst", "completed")
                        message_buffer.update_agent_status("Portfolio Manager", "completed")

            # Update the display
            update_display(layout, stats_handler=stats_handler, start_time=start_time)

            trace.append(chunk)

        # Get final state and decision
        final_state = trace[-1]
        decision = graph.process_signal(final_state["final_trade_decision"])

        # Update all agent statuses to completed
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "System", f"Completed analysis for {selections['analysis_date']}"
        )

        # Update final report sections
        for section in message_buffer.report_sections.keys():
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    # Post-analysis prompts (outside Live context for clean interaction)
    console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")

    # Prompt to save report
    save_choice = typer.prompt("Save report?", default="Y").strip().upper()
    if save_choice in ("Y", "YES", ""):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = Path.cwd() / "reports" / f"{selections['ticker']}_{timestamp}"
        save_path_str = typer.prompt(
            "Save path (press Enter for default)",
            default=str(default_path)
        ).strip()
        save_path = Path(save_path_str)
        try:
            report_file = save_report_to_disk(final_state, selections["ticker"], save_path)
            console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
            console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
        except Exception as e:
            console.print(f"[red]Error saving report: {e}[/red]")

    # Prompt to display full report
    display_choice = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper()
    if display_choice in ("Y", "YES", ""):
        display_complete_report(final_state)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        run_analysis()


def _parse_symbols(symbols: str) -> list[str]:
    parsed = [normalize_ticker_symbol(symbol) for symbol in symbols.split(",") if symbol.strip()]
    if not parsed:
        raise typer.BadParameter("At least one symbol is required.")
    return parsed


def _build_store(execution_config) -> SQLitePersistence:
    return SQLitePersistence(execution_config.db_path)


def _build_broker(*, required: bool, logger=None):
    try:
        return AlpacaPaperBroker.from_env(logger=logger)
    except BrokerConfigurationError:
        if required:
            raise
        return None


def _build_trading_cycle_runner(*, execute: bool, require_broker: bool) -> TradingCycleRunner:
    execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=execute)
    risk_config = load_risk_config()
    store = _build_store(execution_config)
    analysis_engine = TradingAgentsAnalysisEngine(execution_config)
    broker = _build_broker(required=require_broker)
    return TradingCycleRunner(
        execution_config=execution_config,
        risk_config=risk_config,
        store=store,
        analysis_engine=analysis_engine,
        broker=broker,
    )


def _collect_llm_overrides(
    *,
    llm_provider: str | None,
    deep_model: str | None,
    quick_model: str | None,
    backend_url: str | None,
    openai_reasoning_effort: str | None,
    google_thinking_level: str | None,
    anthropic_effort: str | None,
) -> dict[str, str]:
    return {
        "llm_provider": llm_provider.lower() if llm_provider else None,
        "deep_think_llm": deep_model,
        "quick_think_llm": quick_model,
        "backend_url": backend_url,
        "openai_reasoning_effort": openai_reasoning_effort,
        "google_thinking_level": google_thinking_level,
        "anthropic_effort": anthropic_effort,
    }


def _build_trading_cycle_runner_with_overrides(
    *,
    execute: bool,
    require_broker: bool,
    llm_overrides: dict[str, str] | None,
) -> TradingCycleRunner:
    execution_config = load_execution_config(
        project_dir=str(Path.cwd()),
        execute=execute,
        llm_overrides=llm_overrides,
    )
    risk_config = load_risk_config()
    store = _build_store(execution_config)
    analysis_engine = TradingAgentsAnalysisEngine(execution_config)
    broker = _build_broker(required=require_broker)
    return TradingCycleRunner(
        execution_config=execution_config,
        risk_config=risk_config,
        store=store,
        analysis_engine=analysis_engine,
        broker=broker,
    )


def _build_replay_runner() -> tuple[ReplayRunner, SQLitePersistence]:
    execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=False)
    risk_config = load_risk_config()
    store = _build_store(execution_config)
    analysis_engine = TradingAgentsAnalysisEngine(execution_config)
    runner = ReplayRunner(
        execution_config=execution_config,
        risk_config=risk_config,
        store=store,
        analysis_engine=analysis_engine,
    )
    return runner, store


def _build_replay_runner_with_overrides(
    *,
    llm_overrides: dict[str, str] | None,
) -> tuple[ReplayRunner, SQLitePersistence]:
    execution_config = load_execution_config(
        project_dir=str(Path.cwd()),
        execute=False,
        llm_overrides=llm_overrides,
    )
    risk_config = load_risk_config()
    store = _build_store(execution_config)
    analysis_engine = TradingAgentsAnalysisEngine(execution_config)
    runner = ReplayRunner(
        execution_config=execution_config,
        risk_config=risk_config,
        store=store,
        analysis_engine=analysis_engine,
    )
    return runner, store


def _build_daemon_service_with_overrides(
    *,
    llm_overrides: dict[str, str] | None,
) -> tuple[DaemonService, SQLitePersistence]:
    execution_config = load_execution_config(
        project_dir=str(Path.cwd()),
        execute=True,
        llm_overrides=llm_overrides,
    )
    risk_config = load_risk_config()
    store = _build_store(execution_config)
    analysis_engine = TradingAgentsAnalysisEngine(execution_config)
    broker = _build_broker(required=False)
    runner = TradingCycleRunner(
        execution_config=execution_config,
        risk_config=risk_config,
        store=store,
        analysis_engine=analysis_engine,
        broker=broker,
    )
    return (
        DaemonService(
            execution_config=execution_config,
            store=store,
            runner=runner,
            broker=broker,
        ),
        store,
    )

def _build_dashboard_data_service(
    *,
    refresh_seconds: int,
) -> object:
    return build_dashboard_data_service(
        refresh_seconds=refresh_seconds,
        project_dir=str(Path.cwd()),
    )


def _render_cycle_result(result) -> None:
    summary = Table(title=f"{result.mode.value} summary")
    summary.add_column("Symbol", style="cyan")
    summary.add_column("Status", style="green")
    summary.add_column("Risk", style="yellow")
    summary.add_column("Order", style="magenta")
    summary.add_column("Error", style="red")

    for item in result.symbol_results:
        risk_text = ", ".join(item.risk_decision.reasons[:2]) if item.risk_decision else "-"
        order_text = (
            f"{item.submitted_order.side.value} {item.submitted_order.qty or item.submitted_order.notional_usd or '-'}"
            if item.submitted_order
            else "-"
        )
        error_text = item.error or "-"
        summary.add_row(item.symbol, item.execution_status, risk_text, order_text, error_text)

    console.print(summary)
    console.print(f"DB: {result.db_path}")
    console.print(f"Logs: {result.log_path}")
    console.print(f"Audit: {result.audit_path}")
    console.print(f"Results: {result.result_path}")


def _render_account(account) -> None:
    table = Table(title="Alpaca Paper Account")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Account ID", account.account_id or "-")
    table.add_row("Status", account.status or "-")
    table.add_row("Cash", f"${account.cash:,.2f}")
    table.add_row("Equity", f"${account.equity:,.2f}")
    table.add_row("Buying Power", f"${account.buying_power:,.2f}")
    table.add_row("Paper Endpoint", "Yes" if account.paper else "No")
    console.print(table)


def _render_positions(positions) -> None:
    table = Table(title="Positions")
    table.add_column("Symbol", style="cyan")
    table.add_column("Qty", style="green")
    table.add_column("Avg Entry", style="yellow")
    table.add_column("Market Value", style="magenta")
    table.add_column("Unrealized P/L", style="white")
    for position in positions:
        table.add_row(
            position.symbol,
            f"{position.qty:.4f}",
            f"${(position.avg_entry_price or 0.0):,.2f}",
            f"${(position.market_value or 0.0):,.2f}",
            f"${(position.unrealized_pl or 0.0):,.2f}",
        )
    console.print(table)


def _render_orders(orders) -> None:
    table = Table(title="Orders")
    table.add_column("Submitted", style="cyan")
    table.add_column("Symbol", style="green")
    table.add_column("Side", style="yellow")
    table.add_column("Status", style="magenta")
    table.add_column("Qty/Notional", style="white")
    for order in orders:
        size = f"{order.qty:.4f}" if order.qty else f"${(order.notional_usd or 0.0):,.2f}"
        submitted = order.submitted_at.isoformat() if order.submitted_at else "-"
        table.add_row(submitted, order.symbol, order.side.value, order.status, size)
    console.print(table)


def _render_daemon_status(status) -> None:
    table = Table(title="Daemon Status")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Running", "Yes" if status.running else "No")
    table.add_row("PID", str(status.pid or "-"))
    table.add_row(
        "Last Heartbeat",
        status.last_heartbeat_at.isoformat() if status.last_heartbeat_at else "-",
    )
    table.add_row(
        "Last Cycle Start",
        status.last_cycle_started_at.isoformat() if status.last_cycle_started_at else "-",
    )
    table.add_row(
        "Last Cycle End",
        status.last_cycle_completed_at.isoformat() if status.last_cycle_completed_at else "-",
    )
    table.add_row("Last Bucket", status.last_cycle_bucket or "-")
    table.add_row("Paused", "Yes" if status.paused else "No")
    table.add_row("Stop Requested", "Yes" if status.stop_requested else "No")
    table.add_row("Symbols Processed", ", ".join(status.symbols_processed) or "-")
    table.add_row("Last Error", status.last_error or "-")
    table.add_row("Trades Today", str(status.trades_today))
    table.add_row(
        "Trades/Symbol Today",
        ", ".join(f"{symbol}:{count}" for symbol, count in status.trades_per_symbol_today.items()) or "-",
    )
    table.add_row(
        "Daily Trade Cap",
        "Disabled" if not status.daily_trade_cap_enabled else "Reached" if status.daily_trade_cap_reached else "Active",
    )
    if status.account:
        table.add_row("Cash", f"${status.account.cash:,.2f}")
        table.add_row("Equity", f"${status.account.equity:,.2f}")
    table.add_row("Open Positions", str(len(status.open_positions)))
    table.add_row("Learning Summary", status.learning_summary or "-")
    performance = status.performance_snapshot or {}
    if performance:
        table.add_row("Account Value", f"${performance.get('account_value', 0.0):,.2f}")
        table.add_row("Realized PnL", f"${performance.get('realized_pnl', 0.0):,.2f}")
        table.add_row("Unrealized PnL", f"${performance.get('unrealized_pnl', 0.0):,.2f}")
        win_rate = performance.get("win_rate")
        table.add_row("Win Rate", f"{win_rate:.1%}" if isinstance(win_rate, (int, float)) else "-")
    console.print(table)


def _render_learning_state(state) -> None:
    if not state:
        console.print("No learning state recorded yet.")
        return
    table = Table(title="Agent Learning State")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Learning Summary", state.get("learning_summary") or "-")
    table.add_row(
        "Recurring Mistakes",
        "\n".join(state.get("recurring_mistakes") or []) or "-",
    )
    table.add_row(
        "Recurring Successes",
        "\n".join(state.get("recurring_success_patterns") or []) or "-",
    )
    table.add_row(
        "Recent Lessons",
        "\n".join(state.get("recent_lessons") or []) or "-",
    )
    console.print(table)


def _print_cli_error(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


@app.command("dry-run")
def dry_run(
    symbols: str = typer.Option(..., help="Comma-separated symbols, e.g. NVDA,AAPL"),
    date: str = typer.Option(..., help="Analysis date in YYYY-MM-DD format"),
    llm_provider: str = typer.Option(None, help="Override provider: openai, google, anthropic, xai, openrouter, ollama"),
    deep_model: str = typer.Option(None, help="Override deep-thinking model"),
    quick_model: str = typer.Option(None, help="Override quick-thinking model"),
    backend_url: str = typer.Option(None, help="Override provider base URL"),
    openai_reasoning_effort: str = typer.Option(None, help="Override OpenAI reasoning effort"),
    google_thinking_level: str = typer.Option(None, help="Override Google thinking level"),
    anthropic_effort: str = typer.Option(None, help="Override Anthropic effort"),
):
    try:
        runner = _build_trading_cycle_runner_with_overrides(
            execute=False,
            require_broker=False,
            llm_overrides=_collect_llm_overrides(
                llm_provider=llm_provider,
                deep_model=deep_model,
                quick_model=quick_model,
                backend_url=backend_url,
                openai_reasoning_effort=openai_reasoning_effort,
                google_thinking_level=google_thinking_level,
                anthropic_effort=anthropic_effort,
            ),
        )
        result = runner.run_cycle(
            symbols=_parse_symbols(symbols),
            analysis_date=date,
            mode=RunMode.DRY_RUN,
            execute=False,
        )
        _render_cycle_result(result)
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command("paper-run")
def paper_run(
    symbols: str = typer.Option(..., help="Comma-separated symbols, e.g. NVDA,AAPL"),
    date: str = typer.Option(..., help="Analysis date in YYYY-MM-DD format"),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually submit orders to Alpaca paper trading after risk approval.",
    ),
    llm_provider: str = typer.Option(None, help="Override provider: openai, google, anthropic, xai, openrouter, ollama"),
    deep_model: str = typer.Option(None, help="Override deep-thinking model"),
    quick_model: str = typer.Option(None, help="Override quick-thinking model"),
    backend_url: str = typer.Option(None, help="Override provider base URL"),
    openai_reasoning_effort: str = typer.Option(None, help="Override OpenAI reasoning effort"),
    google_thinking_level: str = typer.Option(None, help="Override Google thinking level"),
    anthropic_effort: str = typer.Option(None, help="Override Anthropic effort"),
):
    try:
        runner = _build_trading_cycle_runner_with_overrides(
            execute=execute,
            require_broker=execute,
            llm_overrides=_collect_llm_overrides(
                llm_provider=llm_provider,
                deep_model=deep_model,
                quick_model=quick_model,
                backend_url=backend_url,
                openai_reasoning_effort=openai_reasoning_effort,
                google_thinking_level=google_thinking_level,
                anthropic_effort=anthropic_effort,
            ),
        )
        result = runner.run_cycle(
            symbols=_parse_symbols(symbols),
            analysis_date=date,
            mode=RunMode.PAPER,
            execute=execute,
        )
        _render_cycle_result(result)
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command()
def account():
    try:
        broker = _build_broker(required=True)
        _render_account(broker.get_account())
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command()
def positions():
    try:
        broker = _build_broker(required=True)
        _render_positions(broker.list_positions())
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command()
def orders(
    status: str = typer.Option("all", help="Order status filter: open, closed, all"),
    limit: int = typer.Option(20, help="Number of orders to show"),
):
    try:
        broker = _build_broker(required=True)
        _render_orders(broker.list_orders(status=status, limit=limit))
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command()
def pnl(
    limit: int = typer.Option(30, help="Number of recent daily PnL rows to show"),
):
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=False)
        store = _build_store(execution_config)
        rows = store.get_recent_pnl(limit=limit)
        table = Table(title="Daily PnL")
        table.add_column("Date", style="cyan")
        table.add_column("Equity", style="green")
        table.add_column("Cash", style="yellow")
        table.add_column("Realized", style="magenta")
        table.add_column("Unrealized", style="white")
        table.add_column("Gross Exposure", style="blue")
        for row in rows:
            table.add_row(
                row["trade_date"],
                f"${row['equity']:,.2f}",
                f"${row['cash']:,.2f}",
                f"${row['realized_pnl']:,.2f}",
                f"${row['unrealized_pnl']:,.2f}",
                f"${row['gross_exposure']:,.2f}",
            )
        console.print(table)
        console.print(f"DB: {store.db_path}")
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command()
def replay(
    symbols: str = typer.Option(..., help="Comma-separated symbols, e.g. NVDA,AAPL"),
    from_date: str = typer.Option(..., "--from", help="Replay start date YYYY-MM-DD"),
    to_date: str = typer.Option(..., "--to", help="Replay end date YYYY-MM-DD"),
    llm_provider: str = typer.Option(None, help="Override provider: openai, google, anthropic, xai, openrouter, ollama"),
    deep_model: str = typer.Option(None, help="Override deep-thinking model"),
    quick_model: str = typer.Option(None, help="Override quick-thinking model"),
    backend_url: str = typer.Option(None, help="Override provider base URL"),
    openai_reasoning_effort: str = typer.Option(None, help="Override OpenAI reasoning effort"),
    google_thinking_level: str = typer.Option(None, help="Override Google thinking level"),
    anthropic_effort: str = typer.Option(None, help="Override Anthropic effort"),
):
    try:
        runner, _ = _build_replay_runner_with_overrides(
            llm_overrides=_collect_llm_overrides(
                llm_provider=llm_provider,
                deep_model=deep_model,
                quick_model=quick_model,
                backend_url=backend_url,
                openai_reasoning_effort=openai_reasoning_effort,
                google_thinking_level=google_thinking_level,
                anthropic_effort=anthropic_effort,
            )
        )
        result = runner.run(
            symbols=_parse_symbols(symbols),
            from_date=from_date,
            to_date=to_date,
        )
        _render_cycle_result(result)
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("run")
def daemon_run(
    llm_provider: str = typer.Option(None, help="Override provider: openai, google, anthropic, xai, openrouter, ollama"),
    deep_model: str = typer.Option(None, help="Override deep-thinking model"),
    quick_model: str = typer.Option(None, help="Override quick-thinking model"),
    backend_url: str = typer.Option(None, help="Override provider base URL"),
    openai_reasoning_effort: str = typer.Option(None, help="Override OpenAI reasoning effort"),
    google_thinking_level: str = typer.Option(None, help="Override Google thinking level"),
    anthropic_effort: str = typer.Option(None, help="Override Anthropic effort"),
):
    try:
        service, _ = _build_daemon_service_with_overrides(
            llm_overrides=_collect_llm_overrides(
                llm_provider=llm_provider,
                deep_model=deep_model,
                quick_model=quick_model,
                backend_url=backend_url,
                openai_reasoning_effort=openai_reasoning_effort,
                google_thinking_level=google_thinking_level,
                anthropic_effort=anthropic_effort,
            )
        )
        service.run_forever()
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("start")
def daemon_start(
    llm_provider: str = typer.Option(None, help="Override provider: openai, google, anthropic, xai, openrouter, ollama"),
    deep_model: str = typer.Option(None, help="Override deep-thinking model"),
    quick_model: str = typer.Option(None, help="Override quick-thinking model"),
    backend_url: str = typer.Option(None, help="Override provider base URL"),
    openai_reasoning_effort: str = typer.Option(None, help="Override OpenAI reasoning effort"),
    google_thinking_level: str = typer.Option(None, help="Override Google thinking level"),
    anthropic_effort: str = typer.Option(None, help="Override Anthropic effort"),
):
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=True)
        pid_path = Path(execution_config.daemon_pid_path)
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
                _print_cli_error(f"Daemon already running with pid {pid}.")
            except Exception:
                pid_path.unlink(missing_ok=True)

        log_path = Path(execution_config.log_dir) / "daemon.stdout.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, "-m", "cli.main", "daemon", "run"]
        option_map = {
            "--llm-provider": llm_provider,
            "--deep-model": deep_model,
            "--quick-model": quick_model,
            "--backend-url": backend_url,
            "--openai-reasoning-effort": openai_reasoning_effort,
            "--google-thinking-level": google_thinking_level,
            "--anthropic-effort": anthropic_effort,
        }
        for flag, value in option_map.items():
            if value:
                args.extend([flag, value])

        with log_path.open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(
                args,
                cwd=str(Path.cwd()),
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
        console.print(f"Daemon starting with pid {process.pid}")
        console.print(f"Stdout/stderr: {log_path}")
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("status")
def daemon_status():
    try:
        service, _ = _build_daemon_service_with_overrides(llm_overrides=None)
        _render_daemon_status(service.get_status())
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("memory")
def daemon_memory():
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=True)
        store = _build_store(execution_config)
        _render_learning_state(store.get_learning_state(agent_id=execution_config.agent_id))
        snapshots = store.get_recent_performance_snapshots(agent_id=execution_config.agent_id, limit=5)
        if snapshots:
            table = Table(title="Recent Performance Snapshots")
            table.add_column("Date", style="cyan")
            table.add_column("Bucket", style="green")
            table.add_column("Account Value", style="yellow")
            table.add_column("Total PnL", style="magenta")
            table.add_column("Win Rate", style="white")
            for snapshot in snapshots:
                win_rate = snapshot.get("win_rate")
                table.add_row(
                    snapshot.get("trade_date", "-"),
                    snapshot.get("cycle_bucket") or "-",
                    f"${snapshot.get('account_value', 0.0):,.2f}",
                    f"${snapshot.get('total_pnl', 0.0):,.2f}",
                    f"{win_rate:.1%}" if isinstance(win_rate, (int, float)) else "-",
                )
            console.print(table)
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("heartbeat")
def daemon_heartbeat():
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=True)
        heartbeat_path = Path(execution_config.daemon_heartbeat_path)
        if not heartbeat_path.exists():
            _print_cli_error("No daemon heartbeat file found.")
        console.print(heartbeat_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("pause")
def daemon_pause():
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=True)
        store = _build_store(execution_config)
        store.set_paused(True)
        console.print("Daemon paused.")
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("resume")
def daemon_resume():
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=True)
        store = _build_store(execution_config)
        store.set_paused(False)
        store.set_stop_requested(False)
        console.print("Daemon resumed.")
    except Exception as exc:
        _print_cli_error(str(exc))


@daemon_app.command("stop")
def daemon_stop():
    try:
        execution_config = load_execution_config(project_dir=str(Path.cwd()), execute=True)
        store = _build_store(execution_config)
        store.set_stop_requested(True)
        pid_path = Path(execution_config.daemon_pid_path)
        if pid_path.exists():
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            console.print(f"Stop requested for daemon pid {pid}.")
        else:
            console.print("Stop requested; no pid file present.")
    except Exception as exc:
        _print_cli_error(str(exc))


@dashboard_app.command("run")
def dashboard_run(
    host: str = typer.Option("127.0.0.1", help="Bind host. Use 0.0.0.0 for LAN/VPS access."),
    port: int = typer.Option(8000, help="HTTP port for the dashboard."),
    refresh_seconds: int = typer.Option(
        5,
        help="Dashboard polling interval in seconds.",
        min=1,
    ),
):
    try:
        data_service = _build_dashboard_data_service(refresh_seconds=refresh_seconds)
        server = DashboardServer(
            data_service=data_service,
            host=host,
            port=port,
        )
        console.print(f"Dashboard listening on http://{host}:{port}")
        if host == "0.0.0.0":
            console.print(f"Local browser URL: http://127.0.0.1:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("Dashboard stopped.")
    except Exception as exc:
        _print_cli_error(str(exc))


@app.command()
def analyze():
    run_analysis()


if __name__ == "__main__":
    app()
