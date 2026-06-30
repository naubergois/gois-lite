import React, { useState, useRef, useEffect } from 'react';
import './ControlsPanel.css';

interface DropdownOption {
  id: string;
  label: string;
}

interface Book {
  id: string;
  title: string;
  author?: string;
  subtitle?: string;
  category?: string;
}

interface Course {
  id: string;
  title: string;
  description?: string;
  duration?: string;
}

interface ControlsPanelProps {
  onTeamChange?: (value: string) => void;
  onModelChange?: (value: string) => void;
  onOutputChange?: (value: string) => void;
  onTokensChange?: (value: string) => void;
  onButtonClick?: (buttonLabel: string) => void;
}

/**
 * Modern Controls Panel Component
 * Reusable, accessible component with dropdowns and action buttons
 */
export const ControlsPanel: React.FC<ControlsPanelProps> = ({
  onTeamChange,
  onModelChange,
  onOutputChange,
  onTokensChange,
  onButtonClick,
}) => {
  const [isExpanded, setIsExpanded] = useState(true);
  const [selectedTeam, setSelectedTeam] = useState('artigo-haron');
  const [selectedModel, setSelectedModel] = useState('deepseek-chat');
  const [selectedOutput, setSelectedOutput] = useState('auto');
  const [selectedTokens, setSelectedTokens] = useState('balanced');
  const [showTeamModal, setShowTeamModal] = useState(false);
  const [teamBooks, setTeamBooks] = useState<Book[]>([]);
  const [teamCourses, setTeamCourses] = useState<Course[]>([]);
  const [loadingTeamData, setLoadingTeamData] = useState(false);

  const teams: DropdownOption[] = [
    { id: 'sem-time', label: 'Sem time' },
    { id: 'artigo-haron', label: 'Artigo Haron' },
    { id: 'deepseekchat', label: 'DeepSeek Chat' },
  ];

  const models: DropdownOption[] = [
    { id: 'deepseek-chat', label: 'DeepSeek Chat' },
    { id: 'gpt-4', label: 'GPT-4' },
    { id: 'claude-3', label: 'Claude 3' },
  ];

  const outputOptions: DropdownOption[] = [
    { id: 'auto', label: 'Auto' },
    { id: 'agent', label: 'Agente' },
    { id: 'kanban', label: 'Kanban' },
    { id: 'task', label: 'Task' },
  ];

  const tokenOptions: DropdownOption[] = [
    { id: 'economy', label: 'Econômico' },
    { id: 'balanced', label: 'Balanceado' },
    { id: 'full', label: 'Completo' },
    { id: 'debug', label: 'Debug' },
  ];

  const handleTeamChange = (value: string) => {
    setSelectedTeam(value);
    onTeamChange?.(value);
    loadTeamData();
    setShowTeamModal(true);
  };

  const loadTeamData = async () => {
    setLoadingTeamData(true);
    try {
      // Fetch books
      const booksRes = await fetch('/books?summary=true');
      if (booksRes.ok) {
        const booksData = await booksRes.json();
        setTeamBooks(Array.isArray(booksData) ? booksData : []);
      }

      // Fetch courses
      const coursesRes = await fetch('/courses');
      if (coursesRes.ok) {
        const coursesData = await coursesRes.json();
        setTeamCourses(
          Array.isArray(coursesData)
            ? coursesData
            : (coursesData.courses || [])
        );
      }
    } catch (error) {
      console.error('Error loading team data:', error);
    } finally {
      setLoadingTeamData(false);
    }
  };

  const handleModelChange = (value: string) => {
    setSelectedModel(value);
    onModelChange?.(value);
  };

  const handleOutputChange = (value: string) => {
    setSelectedOutput(value);
    onOutputChange?.(value);
  };

  const handleTokensChange = (value: string) => {
    setSelectedTokens(value);
    onTokensChange?.(value);
  };

  const handleButtonClick = (label: string) => {
    onButtonClick?.(label);
  };

  return (
    <div className="controls-panel">
      <div className="controls-header">
        <button
          className="controls-toggle"
          onClick={() => setIsExpanded(!isExpanded)}
          aria-expanded={isExpanded}
          title="Expandir/Colapsar controles"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        <h3 className="controls-title">
          <span className="controls-icon">⚙️</span>
          Controles
        </h3>
      </div>

      <div className={`controls-content ${isExpanded ? '' : 'collapsed'}`}>
        {/* Dropdowns Row */}
        <div className="controls-row">
          <SelectDropdown
            label="Time"
            value={selectedTeam}
            options={teams}
            onChange={handleTeamChange}
          />
          <SelectDropdown
            label="Modelo"
            value={selectedModel}
            options={models}
            onChange={handleModelChange}
          />
          <SelectDropdown
            label="Saída"
            value={selectedOutput}
            options={outputOptions}
            onChange={handleOutputChange}
          />
          <SelectDropdown
            label="Tokens"
            value={selectedTokens}
            options={tokenOptions}
            onChange={handleTokensChange}
          />
        </div>

        {/* Action Buttons Row 1 */}
        <div className="controls-row buttons">
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Minicurrículo')}
            title="Gerar minicurrículo profissional com preview"
          >
            📇 Minicurrículo
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Replicate')}
            title="Gerar imagem ou vídeo com modelos Replicate"
          >
            🎬 Replicate
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Fala')}
            title="Ler respostas em voz alta"
          >
            Fala: off
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('OpenClaw')}
            title="Liga/desliga a conexão com o OpenClaw"
          >
            OpenClaw: on
          </button>
          <button
            className="btn btn-secondary btn-swarm"
            onClick={() => handleButtonClick('Swarm')}
            title="Modo Swarm — orquestração RuFlo"
          >
            🌀 Swarm: off
          </button>
        </div>

        {/* Action Buttons Row 2 */}
        <div className="controls-row buttons">
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Anexar')}
            title="Anexar ficheiros de texto, imagem ou áudio"
          >
            Anexar
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Docs')}
            title="Enviar documentos fixos ao contexto"
          >
            Docs
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Acadêmico')}
            title="Ferramentas acadêmicas — template LaTeX"
          >
            📚
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Personagem')}
            title="Enviar foto para cadastrar personagem"
          >
            👤 Personagem
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => handleButtonClick('Personagens')}
            title="Buscar personagens cadastrados"
          >
            🔍 Personagens
          </button>
        </div>

        <div className="controls-info">
          <span className="info-badge">MCP: -55 ativas</span>
          <span className="info-badge">73 Saída ligadas</span>
          <span className="info-badge">310 catálogo</span>
        </div>
      </div>

      {/* Team Modal */}
      {showTeamModal && (
        <div className="modal-overlay" onClick={() => setShowTeamModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 className="modal-title">
                Livros e Cursos - {selectedTeam}
              </h2>
              <button
                className="modal-close"
                onClick={() => setShowTeamModal(false)}
                title="Fechar"
              >
                ✕
              </button>
            </div>

            {loadingTeamData ? (
              <div className="modal-loading">
                <div className="spinner"></div>
                Carregando...
              </div>
            ) : (
              <div className="modal-body">
                {/* Books Section */}
                <div className="modal-section">
                  <h3 className="section-title">📚 Livros ({teamBooks.length})</h3>
                  {teamBooks.length > 0 ? (
                    <div className="items-grid">
                      {teamBooks.map((book) => (
                        <div key={book.id} className="item-card">
                          <div className="item-title">{book.title}</div>
                          {book.subtitle && (
                            <div className="item-subtitle">{book.subtitle}</div>
                          )}
                          {book.author && (
                            <div className="item-meta">Por: {book.author}</div>
                          )}
                          {book.category && (
                            <div className="item-category">{book.category}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="empty-state">Nenhum livro disponível</p>
                  )}
                </div>

                {/* Courses Section */}
                <div className="modal-section">
                  <h3 className="section-title">🎓 Cursos ({teamCourses.length})</h3>
                  {teamCourses.length > 0 ? (
                    <div className="items-grid">
                      {teamCourses.map((course) => (
                        <div key={course.id} className="item-card">
                          <div className="item-title">{course.title}</div>
                          {course.description && (
                            <div className="item-description">
                              {course.description}
                            </div>
                          )}
                          {course.duration && (
                            <div className="item-meta">Duração: {course.duration}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="empty-state">Nenhum curso disponível</p>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

/**
 * Reusable Select Dropdown Component
 */
interface SelectDropdownProps {
  label: string;
  value: string;
  options: DropdownOption[];
  onChange: (value: string) => void;
}

const SelectDropdown: React.FC<SelectDropdownProps> = ({ label, value, options, onChange }) => {
  const [isOpen, setIsOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const selectedLabel = options.find(opt => opt.id === value)?.label || label;

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (buttonRef.current && !buttonRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };

    if (isOpen) {
      document.addEventListener('click', handleClickOutside);
      return () => document.removeEventListener('click', handleClickOutside);
    }
  }, [isOpen]);

  return (
    <div className="select-dropdown">
      <label className="select-label">{label}</label>
      <div className="select-wrapper">
        <button
          ref={buttonRef}
          className="select-button"
          onClick={() => setIsOpen(!isOpen)}
          aria-expanded={isOpen}
        >
          <span className="select-button-label">{selectedLabel}</span>
          <svg className="select-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        {isOpen && (
          <div className="select-menu">
            {options.map(option => (
              <button
                key={option.id}
                className={`select-option ${option.id === value ? 'selected' : ''}`}
                onClick={() => {
                  onChange(option.id);
                  setIsOpen(false);
                }}
              >
                {option.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default ControlsPanel;
