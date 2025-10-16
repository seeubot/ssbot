// server.js
const express = require('express');
const mongoose = require('mongoose');
const cors = require('cors');
const { Telegraf } = require('telegraf');
const axios = require('axios');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(cors());
app.use(express.json());
app.use('/thumbnails', express.static('thumbnails'));

// Ensure thumbnails directory exists
if (!fs.existsSync('thumbnails')) {
    fs.mkdirSync('thumbnails');
}

// MongoDB Models
const ContentSchema = new mongoose.Schema({
    type: { type: String, enum: ['movie', 'webseries'], required: true },
    title: { type: String, required: true },
    thumbnail: { type: String, required: true },
    streamingLinks: [String],
    episodes: [{
        episodeNumber: Number,
        title: String,
        streamingLink: String
    }],
    createdAt: { type: Date, default: Date.now }
});

const Content = mongoose.model('Content', ContentSchema);

// Telegram Bot
const bot = new Telegraf(process.env.TELEGRAM_BOT_TOKEN);

// Store user states for conversation flow
const userStates = new Map();

// Download thumbnail from Telegram
async function downloadThumbnail(fileUrl, filename) {
    try {
        const response = await axios({
            method: 'GET',
            url: fileUrl,
            responseType: 'stream',
        });

        const filePath = path.join('thumbnails', filename);
        const writer = fs.createWriteStream(filePath);
        
        response.data.pipe(writer);
        
        return new Promise((resolve, reject) => {
            writer.on('finish', () => resolve(`/thumbnails/${filename}`));
            writer.on('error', reject);
        });
    } catch (error) {
        throw new Error('Failed to download thumbnail');
    }
}

// Bot commands and handlers
bot.start((ctx) => {
    const keyboard = {
        reply_markup: {
            keyboard: [
                ['🎬 Add Movie', '📺 Add Web Series'],
                ['📋 View Content', '✏️ Edit Content']
            ],
            resize_keyboard: true,
            one_time_keyboard: false
        }
    };
    ctx.reply('Welcome to Content Manager Bot! Choose an option:', keyboard);
});

bot.hears('🎬 Add Movie', (ctx) => {
    userStates.set(ctx.from.id, { 
        type: 'movie', 
        step: 'title',
        streamingLinks: []
    });
    ctx.reply('Please send the movie title:');
});

bot.hears('📺 Add Web Series', (ctx) => {
    userStates.set(ctx.from.id, { 
        type: 'webseries', 
        step: 'title',
        episodes: []
    });
    ctx.reply('Please send the web series title:');
});

bot.hears('📋 View Content', async (ctx) => {
    try {
        const content = await Content.find().sort({ createdAt: -1 }).limit(10);
        if (content.length === 0) {
            ctx.reply('No content available yet.');
            return;
        }

        let message = '📋 Recent Content:\n\n';
        content.forEach((item, index) => {
            message += `${index + 1}. ${item.title} (${item.type})\n`;
            if (item.type === 'webseries') {
                message += `   Episodes: ${item.episodes.length}\n`;
            }
            message += '\n';
        });

        ctx.reply(message);
    } catch (error) {
        ctx.reply('Error fetching content.');
    }
});

bot.hears('✏️ Edit Content', async (ctx) => {
    try {
        const content = await Content.find().sort({ createdAt: -1 }).limit(5);
        if (content.length === 0) {
            ctx.reply('No content available to edit.');
            return;
        }

        const keyboard = {
            inline_keyboard: content.map(item => [
                { 
                    text: `${item.title} (${item.type})`, 
                    callback_data: `edit_${item._id}` 
                }
            ])
        };

        ctx.reply('Select content to edit:', { reply_markup: keyboard });
    } catch (error) {
        ctx.reply('Error fetching content for editing.');
    }
});

bot.on('message', async (ctx) => {
    const userId = ctx.from.id;
    const state = userStates.get(userId);
    const message = ctx.message;

    if (!state) return;

    switch (state.step) {
        case 'title':
            if (message.text) {
                userStates.set(userId, { 
                    ...state, 
                    title: message.text, 
                    step: 'thumbnail' 
                });
                ctx.reply('Great! Now please send the thumbnail image:');
            }
            break;

        case 'thumbnail':
            if (message.photo) {
                try {
                    const fileId = message.photo[message.photo.length - 1].file_id;
                    const file = await ctx.telegram.getFile(fileId);
                    const fileUrl = `https://api.telegram.org/file/bot${process.env.TELEGRAM_BOT_TOKEN}/${file.file_path}`;
                    
                    const filename = `thumbnail_${Date.now()}${path.extname(file.file_path)}`;
                    const thumbnailPath = await downloadThumbnail(fileUrl, filename);
                    
                    userStates.set(userId, { 
                        ...state, 
                        thumbnail: thumbnailPath,
                        step: state.type === 'movie' ? 'movie_links' : 'episode_count'
                    });

                    if (state.type === 'movie') {
                        ctx.reply('Thumbnail received! Now please send streaming links (one link per message). Send /done when finished:');
                    } else {
                        ctx.reply('Thumbnail received! How many episodes does this web series have?');
                    }
                } catch (error) {
                    ctx.reply('Error downloading thumbnail. Please try again.');
                }
            } else {
                ctx.reply('Please send an image for the thumbnail.');
            }
            break;

        case 'movie_links':
            if (message.text === '/done') {
                if (state.streamingLinks.length === 0) {
                    ctx.reply('Please add at least one streaming link before finishing.');
                    return;
                }
                
                try {
                    const content = new Content({
                        type: state.type,
                        title: state.title,
                        thumbnail: state.thumbnail,
                        streamingLinks: state.streamingLinks
                    });
                    await content.save();
                    
                    ctx.reply('✅ Movie added successfully!');
                    userStates.delete(userId);
                } catch (error) {
                    ctx.reply('Error saving movie. Please try again.');
                }
            } else if (message.text) {
                const links = state.streamingLinks || [];
                links.push(message.text);
                userStates.set(userId, { ...state, streamingLinks: links });
                ctx.reply(`Link added! ${links.length} link(s) so far. Send another link or /done to finish.`);
            }
            break;

        case 'episode_count':
            if (message.text && !isNaN(message.text)) {
                const episodeCount = parseInt(message.text);
                userStates.set(userId, { 
                    ...state, 
                    episodeCount: episodeCount,
                    currentEpisode: 1,
                    step: 'episode_title'
                });
                ctx.reply(`Starting with episode 1. Please send the title for episode 1:`);
            } else {
                ctx.reply('Please send a valid number for episode count.');
            }
            break;

        case 'episode_title':
            if (message.text) {
                userStates.set(userId, { 
                    ...state, 
                    currentEpisodeTitle: message.text,
                    step: 'episode_link'
                });
                ctx.reply(`Now please send the streaming link for episode ${state.currentEpisode}:`);
            }
            break;

        case 'episode_link':
            if (message.text) {
                const episodes = state.episodes || [];
                episodes.push({
                    episodeNumber: state.currentEpisode,
                    title: state.currentEpisodeTitle,
                    streamingLink: message.text
                });

                if (state.currentEpisode >= state.episodeCount) {
                    // All episodes added
                    try {
                        const content = new Content({
                            type: state.type,
                            title: state.title,
                            thumbnail: state.thumbnail,
                            episodes: episodes
                        });
                        await content.save();
                        
                        ctx.reply(`✅ Web series "${state.title}" added successfully with ${episodes.length} episodes!`);
                        userStates.delete(userId);
                    } catch (error) {
                        ctx.reply('Error saving web series. Please try again.');
                    }
                } else {
                    // Move to next episode
                    const nextEpisode = state.currentEpisode + 1;
                    userStates.set(userId, { 
                        ...state, 
                        episodes: episodes,
                        currentEpisode: nextEpisode,
                        step: 'episode_title'
                    });
                    ctx.reply(`Now for episode ${nextEpisode}. Please send the title:`);
                }
            } else {
                ctx.reply('Please send a valid streaming link.');
            }
            break;
    }
});

// Edit content callback
bot.on('callback_query', async (ctx) => {
    const data = ctx.callbackQuery.data;
    
    if (data.startsWith('edit_')) {
        const contentId = data.replace('edit_', '');
        
        try {
            const content = await Content.findById(contentId);
            if (!content) {
                ctx.reply('Content not found.');
                return;
            }

            const keyboard = {
                inline_keyboard: [
                    [{ text: '✏️ Edit Title', callback_data: `edit_title_${contentId}` }],
                    [{ text: '🖼️ Edit Thumbnail', callback_data: `edit_thumbnail_${contentId}` }],
                    content.type === 'movie' ? 
                        [{ text: '🔗 Edit Streaming Links', callback_data: `edit_links_${contentId}` }] :
                        [{ text: '📺 Edit Episodes', callback_data: `edit_episodes_${contentId}` }]
                ]
            };

            ctx.reply(`Editing: ${content.title}\nWhat would you like to edit?`, {
                reply_markup: keyboard
            });
        } catch (error) {
            ctx.reply('Error loading content for editing.');
        }
    }
});

// API Routes
app.get('/api/content', async (req, res) => {
    try {
        const content = await Content.find().sort({ createdAt: -1 });
        res.json(content);
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

app.get('/api/content/:id', async (req, res) => {
    try {
        const content = await Content.findById(req.params.id);
        res.json(content);
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

const PORT = process.env.PORT || 3000;

// Start server
mongoose.connect(process.env.MONGODB_URI || 'mongodb://localhost:27017/telegram-bot')
    .then(() => {
        console.log('Connected to MongoDB');
        app.listen(PORT, () => {
            console.log(`Server running on port ${PORT}`);
            bot.launch();
            console.log('Telegram bot started');
        });
    })
    .catch(err => {
        console.error('MongoDB connection error:', err);
    });

// Enable graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
